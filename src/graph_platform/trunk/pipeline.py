"""Trunk stages for one source: profile -> dictionary -> gated mapping into canonical."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from graph_platform.config import Settings
from graph_platform.contracts import DataDictionary, DomainContract, Expr, GateResult, GateThresholds, MappingSpec, dump_yaml, load_yaml
from graph_platform.storage import Principal, resolve
from graph_platform.trunk import compiler, dictionary, landing
from graph_platform.trunk.profiler import agent_view, profile_source


@dataclass
class RunResult:
    applied: bool
    gates: list[GateResult]
    counts: dict[str, int] = field(default_factory=dict)
    unused_columns: dict[str, list[str]] = field(default_factory=dict)  # source columns left out of the platform

    @property
    def failed(self) -> list[GateResult]:
        return [g for g in self.gates if not g.passed]


def _trunk(tenant: str) -> Principal:
    return Principal(tenant, "platform", "trunk")


def _dirs(settings: Settings, tenant: str, domain: str, source: str) -> tuple[Path, Path, Path]:
    p = _trunk(tenant)
    land = resolve(settings, p, "landing", tenant=tenant, domain=domain) / source
    meta = resolve(settings, p, "meta", tenant=tenant, domain=domain, mode="w") / source
    canon = resolve(settings, p, "canonical", tenant=tenant, domain=domain, mode="w")
    return land, meta, canon


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")


def scan_source(settings: Settings, tenant: str, domain: str, source: str) -> dict[str, landing.SourceTable]:
    """Landing tables of a source, honouring the load mode its onboarding contract declares."""
    land = _dirs(settings, tenant, domain, source)[0]
    contract = load_yaml(DomainContract, settings.tenant(tenant).domains / f"{domain}.yaml")
    decl = next(x for x in contract.sources if x.name == source)
    return landing.scan(land, decl.full_extracts) if land.is_dir() else {}


def profile(settings: Settings, tenant: str, domain: str, source: str) -> dict:
    land, meta, _ = _dirs(settings, tenant, domain, source)
    prof = profile_source(source, domain, scan_source(settings, tenant, domain, source))
    _write_json(meta / "profile.json", prof)
    _write_json(meta / "profile_agent.json", agent_view(prof))
    return prof


def draft_dictionary(settings: Settings, tenant: str, domain: str, source: str, prof: dict | None = None) -> Path:
    path = settings.tenant(tenant).dictionaries / f"{source}.yaml"
    dump_yaml(dictionary.draft(prof or profile(settings, tenant, domain, source)), path)
    return path


def schema_drift(settings: Settings, tenant: str, domain: str, source: str) -> list[dict]:
    """Differences between landing now and the schema of the last applied run."""
    land, meta, _ = _dirs(settings, tenant, domain, source)
    manifest = meta / "manifest.json"
    if not manifest.exists():
        return []
    known = {t: v["columns"] for t, v in json.loads(manifest.read_text())["tables"].items()}
    return landing.drift(scan_source(settings, tenant, domain, source), known)


def _rate(con: duckdb.DuckDBPyConnection, sql: str) -> float:
    v = con.execute(sql).fetchone()[0]
    return 1.0 if v is None else float(v)


def _raw_present(e: Expr) -> str:
    return "coalesce(" + ", ".join(f"CAST({landing.ident(c)} AS VARCHAR)" for c in e.columns()) + ") IS NOT NULL"


def run_mapping(settings: Settings, tenant: str, spec: MappingSpec, *, run_ts: datetime | None = None) -> RunResult:
    """Validate, execute in memory, gate, and only then publish to canonical."""
    o = settings.ontology_of(tenant)
    land, meta, canon = _dirs(settings, tenant, spec.domain, spec.source)
    run_ts = run_ts or datetime.now(UTC).replace(tzinfo=None)
    ts = run_ts.strftime("%Y-%m-%d %H:%M:%S")
    gates: list[GateResult] = []

    def gate(name: str, passed: bool, **detail: object) -> bool:
        gates.append(GateResult(gate=name, passed=passed, detail=detail))
        return passed

    # checked before anything is read or written: a spec can only touch a source its own domain declares
    contract_path = settings.tenant(tenant).domains / f"{spec.domain}.yaml"
    if not contract_path.exists() or spec.source not in [x.name for x in load_yaml(DomainContract, contract_path).sources]:
        gate("spec_valid", False, problems=[f"source '{spec.source}' is not declared by domain '{spec.domain}'"])
        return RunResult(False, gates)
    tables = scan_source(settings, tenant, spec.domain, spec.source)
    problems = compiler.validate(o, spec, tables)
    dict_path = settings.tenant(tenant).dictionaries / f"{spec.source}.yaml"
    if dict_path.exists():
        d = load_yaml(DataDictionary, dict_path)
        problems += dictionary.coverage_problems(d, {t.name: t.columns for t in tables.values()})
    else:
        problems.append(f"dictionary: {dict_path.name} missing")
    if not gate("spec_valid", not problems, problems=problems):
        return RunResult(False, gates)

    con = landing.connect(tables)
    nodes: dict[str, list[str]] = {}
    edges: dict[str, list[str]] = {}
    attached: dict[str, str] = {}
    # what the source has that the platform leaves behind: reported, never silent
    unused = {tm.table: sorted(set(tables[tm.table].columns) - tm.columns_used()) for tm in spec.tables if not tm.attached}
    unused |= {name: sorted(t.columns) for name, t in tables.items() if name not in {tm.table for tm in spec.tables}}
    g = load_yaml(GateThresholds, settings.tenant(tenant).conf / "gates.yaml")
    try:
        for tm in spec.tables:
            if tm.attached:
                personal = [c.name for c in d.table(tm.table).columns if c.pii]
                attached[tm.attached.name] = compiler.attached_sql(o, spec, tm.table, tm.attached, personal)
                ok = _rate(
                    con,
                    f"SELECT avg(({compiler.expr_sql(tm.attached.time)} IS NOT NULL)::INT) "
                    f"FROM {landing.ident(tm.table)} WHERE {_raw_present(tm.attached.time)}",
                )
                gate("time_parse_rate", ok >= g.min_time_parse_rate, table=tm.table, attached=tm.attached.name, rate=ok)
            for n in tm.nodes:
                nodes.setdefault(n.type, []).append(compiler.node_sql(o, spec, tm.table, n, ts))
                key = compiler.expr_sql(n.key)
                if n.unique:
                    null = _rate(con, f"SELECT avg(({key} IS NULL)::INT) FROM {landing.ident(tm.table)} WHERE {compiler.when_sql(n)}")
                    dup = 1 - _rate(
                        con, f"SELECT count(DISTINCT {key}) / count({key}) FROM {landing.ident(tm.table)} WHERE {compiler.when_sql(n)}"
                    )
                    gate("key_null_rate", null <= g.max_key_null_rate, table=tm.table, node=n.type, rate=null)
                    gate("duplicate_rate", dup <= g.max_duplicate_rate, table=tm.table, node=n.type, rate=dup)
                if n.time and n.time.columns():
                    ok = _rate(
                        con,
                        f"SELECT avg(({compiler.expr_sql(n.time)} IS NOT NULL)::INT) "
                        f"FROM {landing.ident(tm.table)} WHERE {_raw_present(n.time)}",
                    )
                    gate("time_parse_rate", ok >= g.min_time_parse_rate, table=tm.table, node=n.type, rate=ok)
            for e in tm.edges:
                sql = compiler.edge_sql(o, spec, tm.table, e, ts)
                dedupe = (
                    f"SELECT * FROM ({sql}) QUALIFY source_pk IS NULL OR row_number() OVER (PARTITION BY source_pk ORDER BY event_time) = 1"
                    if e.key
                    else f"SELECT DISTINCT * FROM ({sql})"
                )
                edges.setdefault(e.type, []).append(dedupe)
                if e.time.columns():
                    ok = _rate(
                        con,
                        f"SELECT avg(({compiler.expr_sql(e.time)} IS NOT NULL)::INT) "
                        f"FROM {landing.ident(tm.table)} WHERE {_raw_present(e.time)}",
                    )
                    gate("time_parse_rate", ok >= g.min_time_parse_rate, table=tm.table, edge=e.type, rate=ok)

        counts = {f"rows_in.{t.name}": t.rows for t in tables.values()}
        for nt, sqls in nodes.items():
            time_col = o.node(nt).time_column
            con.execute(
                f'CREATE TABLE "n_{nt}" AS SELECT * FROM ({" UNION ALL BY NAME ".join(sqls)}) '
                f"QUALIFY row_number() OVER (PARTITION BY node_id ORDER BY {time_col} NULLS LAST) = 1"
            )
            counts[f"nodes.{nt}"] = con.execute(f'SELECT count(*) FROM "n_{nt}"').fetchone()[0]
        all_nodes = " UNION ALL ".join(f'SELECT node_id FROM "n_{nt}"' for nt in nodes) or "SELECT NULL AS node_id"
        con.execute(f"CREATE TABLE _node_ids AS SELECT DISTINCT node_id FROM ({all_nodes})")
        for et, sqls in edges.items():
            con.execute(f'CREATE TABLE "e_{et}" AS SELECT * FROM ({" UNION ALL BY NAME ".join(sqls)})')
            counts[f"edges.{et}"] = con.execute(f'SELECT count(*) FROM "e_{et}"').fetchone()[0]
            cov = _rate(
                con,
                f'SELECT avg((src_id IN (SELECT node_id FROM _node_ids) AND dst_id IN (SELECT node_id FROM _node_ids))::INT) FROM "e_{et}"',
            )
            gate("endpoint_coverage", cov >= g.min_endpoint_coverage, edge=et, rate=cov)
        for name, sql in attached.items():
            con.execute(f'CREATE TABLE "t_{name}" AS {sql}')
            counts[f"tables.{name}"] = con.execute(f'SELECT count(*) FROM "t_{name}"').fetchone()[0]
    except (duckdb.Error, KeyError, ValueError, TypeError, AttributeError) as exc:
        gate("execution", False, error=str(exc))
        return RunResult(False, gates)

    empty = [k for k, v in counts.items() if not k.startswith("rows_in") and v == 0]
    gate("reconciliation", not empty, counts=counts, empty=empty)
    if any(not x.passed for x in gates):
        return RunResult(False, gates, counts)

    for kind, prefix, names in (("nodes", "n_", nodes), ("edges", "e_", edges), ("tables", "t_", attached)):
        for stale in canon.glob(f"{kind}/*/{spec.source}.parquet"):
            stale.unlink()
        for name in names:
            out = canon / kind / name / f"{spec.source}.parquet"
            out.parent.mkdir(parents=True, exist_ok=True)
            con.execute(f"COPY \"{prefix}{name}\" TO '{out}.tmp' (FORMAT parquet)")
            Path(f"{out}.tmp").replace(out)

    dump_yaml(dictionary.annotate(d, spec), dict_path)
    _write_json(
        meta / "manifest.json",
        {
            "source": spec.source,
            "domain": spec.domain,
            "spec_version": spec.version,
            "run_ts": ts,
            "tables": {
                t.name: {"rows": t.rows, "fingerprint": t.fingerprint, "stamp": t.stamp, "columns": t.columns} for t in tables.values()
            },
            "counts": counts,
            "unused_columns": {k: v for k, v in unused.items() if v},
            "gates": [x.model_dump() for x in gates],
        },
    )
    return RunResult(True, gates, counts, {k: v for k, v in unused.items() if v})


def onboard(settings: Settings, tenant: str, domains: list[str] | None = None) -> dict[str, RunResult | None]:
    """Run every declared source of a tenant through the trunk. None = no mapping spec yet."""
    from graph_platform.contracts import DomainContract

    paths = settings.tenant(tenant)
    out: dict[str, RunResult | None] = {}
    for p in sorted(paths.domains.glob("*.yaml")):
        contract = load_yaml(DomainContract, p)
        if domains and contract.domain not in domains:
            continue
        for src in contract.sources:
            prof = profile(settings, tenant, contract.domain, src.name)
            if not (paths.dictionaries / f"{src.name}.yaml").exists():
                draft_dictionary(settings, tenant, contract.domain, src.name, prof)
            spec_path = paths.mappings / f"{src.name}.yaml"
            out[src.name] = run_mapping(settings, tenant, load_yaml(MappingSpec, spec_path)) if spec_path.exists() else None
    return out
