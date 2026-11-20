"""`gp` command line."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from graph_platform.config import Settings
from graph_platform.contracts import SCHEMAS, DomainContract, load_yaml
from graph_platform.ontology import OntologyError

app = typer.Typer(no_args_is_help=True, add_completion=False, pretty_exceptions_enable=False)
ontology_app = typer.Typer(no_args_is_help=True)
schema_app = typer.Typer(no_args_is_help=True)
tenant_app = typer.Typer(no_args_is_help=True)
app.add_typer(ontology_app, name="ontology")
app.add_typer(schema_app, name="schema")
app.add_typer(tenant_app, name="tenant")


@ontology_app.command("validate")
def ontology_validate() -> None:
    try:
        o = Settings.load().ontology
    except OntologyError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"ontology v{o.version}: {len(o.nodes)} node types, {len(o.edges)} edge types")


@schema_app.command("export")
def schema_export(out: Path = typer.Option(Path("conf/schemas"), help="Output directory")) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for name, model in SCHEMAS.items():
        (out / f"{name}.schema.json").write_text(json.dumps(model.model_json_schema(), indent=2))
    typer.echo(f"wrote {len(SCHEMAS)} schemas to {out}")


@tenant_app.command("list")
def tenant_list() -> None:
    s = Settings.load()
    for name in s.tenants():
        domains = sorted(p.stem for p in s.tenant(name).domains.glob("*.yaml"))
        typer.echo(f"{name}: {', '.join(domains)}")


@tenant_app.command("validate")
def tenant_validate(tenant: str) -> None:
    paths = Settings.load().tenant(tenant)
    contracts = [load_yaml(DomainContract, p) for p in sorted(paths.domains.glob("*.yaml"))]
    names = {c.domain for c in contracts}
    problems = [
        f"{c.domain}: file name must equal domain"
        for c, p in zip(contracts, sorted(paths.domains.glob("*.yaml")), strict=True)
        if p.stem != c.domain
    ] + [f"{c.domain}: unknown reader '{r}'" for c in contracts for r in c.readers if r not in names]
    sources = [s.name for c in contracts for s in c.sources]
    problems += [f"duplicate source name '{s}'" for s in sorted(set(sources)) if sources.count(s) > 1]
    if problems:
        typer.echo("\n".join(problems), err=True)
        raise typer.Exit(1)
    typer.echo(f"{tenant}: {len(contracts)} domains, {len(sources)} sources OK")


@app.command("simulate")
def simulate_cmd(
    tenant: str,
    parties: int = typer.Option(20_000),
    seed: int = typer.Option(7),
    extra_source: bool = typer.Option(False, help="Emit only the unseen extra source"),
) -> None:
    from graph_platform.simulator import SimConfig, simulate

    cfg = SimConfig(tenant=tenant, parties=parties, seed=seed)
    for name, rows in simulate(Settings.load(), cfg, extra_source=extra_source).items():
        typer.echo(f"{name}\t{rows}")


def _report(name: str, result) -> bool:
    if result is None:
        typer.echo(f"{name}: no mapping spec")
        return True
    typer.echo(f"{name}: {'APPLIED' if result.applied else 'REJECTED'}")
    for g in result.failed:
        typer.echo(f"  FAIL {g.gate} {json.dumps(g.detail, default=str)[:400]}")
    for k, v in result.counts.items():
        if not k.startswith("rows_in"):
            typer.echo(f"  {k}\t{v}")
    for table, cols in result.unused_columns.items():
        typer.echo(f"  not used: {table}: {', '.join(cols)}")
    return result.applied


@app.command("profile")
def profile_cmd(tenant: str, domain: str, source: str) -> None:
    from graph_platform.trunk import pipeline

    prof = pipeline.profile(Settings.load(), tenant, domain, source)
    for t in prof["tables"]:
        typer.echo(f"{t['name']}\t{t['rows']} rows\t{len(t['columns'])} columns")
    typer.echo(f"{len(prof['foreign_keys'])} foreign key candidates")


@app.command("dictionary")
def dictionary_cmd(tenant: str, domain: str, source: str) -> None:
    """Draft the data dictionary of a source from its profile."""
    from graph_platform.trunk import pipeline

    typer.echo(pipeline.draft_dictionary(Settings.load(), tenant, domain, source))


@app.command("map")
def map_cmd(tenant: str, source: str) -> None:
    from graph_platform.contracts import MappingSpec
    from graph_platform.trunk import pipeline

    s = Settings.load()
    spec = load_yaml(MappingSpec, s.tenant(tenant).mappings / f"{source}.yaml")
    if not _report(source, pipeline.run_mapping(s, tenant, spec)):
        raise typer.Exit(1)


@app.command("extract")
def extract_cmd(tenant: str, domain: list[str] = typer.Option([], help="Only these domains")) -> None:
    """Pull new rows from every connected source database into landing."""
    from graph_platform.trunk import extract

    for source, rows in extract.pull_all(Settings.load(), tenant, domain or None).items():
        typer.echo(f"{source}: " + ", ".join(f"{t} +{n}" for t, n in rows.items()))


@app.command("onboard")
def onboard_cmd(tenant: str) -> None:
    """Profile, describe and map every declared source of a tenant."""
    from graph_platform.trunk import pipeline

    ok = [_report(name, r) for name, r in pipeline.onboard(Settings.load(), tenant).items()]
    if not all(ok):
        raise typer.Exit(1)


@app.command("resolve")
def resolve_cmd(tenant: str, evaluate: bool = typer.Option(False, help="Score against simulator ground truth")) -> None:
    from graph_platform import resolution

    s = Settings.load()
    df = resolution.resolve_parties(s, tenant)
    typer.echo(f"{df.height} party nodes -> {df['global_party_id'].n_unique()} parties")
    if evaluate:
        typer.echo(json.dumps(resolution.evaluate(s, tenant)))


graph_app = typer.Typer(no_args_is_help=True)
app.add_typer(graph_app, name="graph")


@graph_app.command("build")
def graph_build(tenant: str, load: bool = typer.Option(True, help="Also load the graph into Neo4j")) -> None:
    from graph_platform.graph import GraphStore, build_graph, graph_dir

    s = Settings.load()
    for k, v in build_graph(s, tenant).items():
        typer.echo(f"{k}\t{v}")
    if load:
        store = GraphStore(s)
        typer.echo(f"loaded {sum(store.load(tenant, graph_dir(s, tenant)).values())} nodes and edges into Neo4j")
        store.close()


@graph_app.command("query")
def graph_query(tenant: str, name: str, param: list[str] = typer.Option([], help="key=value")) -> None:
    """Run a named Cypher query from graph/queries, scoped to the tenant."""
    from graph_platform.graph import GraphStore, named_query

    store = GraphStore(Settings.load())
    typer.echo(store.query(tenant, named_query(name), dict(p.split("=", 1) for p in param)))
    store.close()


features_app = typer.Typer(no_args_is_help=True)
app.add_typer(features_app, name="features")


@features_app.command("build")
def features_build(
    tenant: str,
    start: str = typer.Option(..., help="First as_of month, YYYY-MM"),
    end: str = typer.Option(..., help="Last as_of month, YYYY-MM"),
    family: list[str] = typer.Option([], help="Families to build; default all"),
) -> None:
    from datetime import date

    from graph_platform import features

    dates = features.month_starts(date.fromisoformat(start + "-01"), date.fromisoformat(end + "-01"))
    counts = features.build(Settings.load(), tenant, dates, family or None)
    typer.echo(f"{len(dates)} snapshots, {len(counts)} family snapshots written")


branch_app = typer.Typer(no_args_is_help=True)
app.add_typer(branch_app, name="branch")


@branch_app.command("run")
def branch_run(tenant: str, name: str, evaluate: bool = typer.Option(False, help="Score against simulator ground truth")) -> None:
    from graph_platform.branches import run_branch

    metrics, out = run_branch(Settings.load(), tenant, name, evaluate=evaluate)
    typer.echo((out / "report.md").read_text())
    typer.echo(f"output: {out}")


@app.command("supervise")
def supervise_cmd(tenant: str) -> None:
    """One supervision pass: agents propose, gates decide, passing changes are applied."""
    from graph_platform.agents.llm import BedrockLLM
    from graph_platform.contracts import AgentsConfig
    from graph_platform.control.audit import Audit
    from graph_platform.control.supervisor import run_once

    s = Settings.load()
    llm = BedrockLLM(load_yaml(AgentsConfig, s.tenant(tenant).agents), Audit(s, tenant))
    for source, outcome in run_once(s, tenant, llm).items():
        typer.echo(f"{source}\t{outcome}")


proposal_app = typer.Typer(no_args_is_help=True)
app.add_typer(proposal_app, name="proposal")


@proposal_app.command("list")
def proposal_list(tenant: str, status: str = typer.Option("needs_human")) -> None:
    from graph_platform.control import proposals

    for p in proposals.load_all(Settings.load(), tenant):
        if p.status == status:
            failed = [g.gate for g in p.gates if not g.passed]
            typer.echo(f"{p.id}\t{p.kind}\t{p.target}\tby {p.author}\tdestructive={p.destructive}\tfailed={failed}")


@proposal_app.command("decide")
def proposal_decide(
    tenant: str, proposal_id: str, approve: bool = typer.Option(..., "--approve/--reject"), by: str = typer.Option(...)
) -> None:
    from graph_platform.control.supervisor import decide

    typer.echo(decide(Settings.load(), tenant, proposal_id, approve, by))


@app.command("audit")
def audit_cmd(tenant: str, last: int = typer.Option(20)) -> None:
    from graph_platform.control.audit import Audit

    for row in Audit(Settings.load(), tenant).rows()[-last:]:
        typer.echo(json.dumps(row))


@app.command("user")
def user_cmd(
    username: str,
    name: str = typer.Option(...),
    role: str = typer.Option(..., help="viewer | analyst | admin"),
    tenant: list[str] = typer.Option(..., help="Tenant the user may open; repeat for several"),
    domain: list[str] = typer.Option([], help="Business unit the user may change; omit for all"),
    password: str = typer.Option(..., prompt=True, hide_input=True, confirmation_prompt=True),
) -> None:
    """Create or replace an account for the web interface."""
    from graph_platform.auth import User, hash_password, save_user

    s = Settings.load()
    save_user(s.users_path, User(username=username, name=name, role=role, tenants=tenant, domains=domain, password=hash_password(password)))
    typer.echo(f"saved {username} ({role})")


@app.command("ui")
def ui_cmd(port: int = typer.Option(8501)) -> None:
    """Open the web interface for business users."""
    import subprocess
    import sys

    script = Path(__file__).with_name("app.py")
    subprocess.run(
        [sys.executable, "-m", "streamlit", "run", str(script), "--server.port", str(port), "--browser.gatherUsageStats", "false"],
        check=True,
    )


if __name__ == "__main__":
    app()
