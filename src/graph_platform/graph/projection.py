"""Project canonical data + resolved parties into one tenant graph (Parquet node/edge tables)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import duckdb

from graph_platform.config import Settings
from graph_platform.storage import Principal, canonical_files, resolve


def _files(paths: list[str]) -> str:
    return "[" + ", ".join("'" + p.replace("'", "''") + "'" for p in paths) + "]"


def graph_dir(settings: Settings, tenant: str, principal: str = "graph", mode: str = "r") -> Path:
    return resolve(settings, Principal(tenant, "platform", principal), "shared", tenant=tenant, mode=mode) / "graph"  # type: ignore[arg-type]


def build_graph(settings: Settings, tenant: str) -> dict[str, int]:
    """Party nodes collapse to global_party_id; every edge endpoint is rewritten accordingly."""
    o = settings.ontology_of(tenant)
    who = Principal(tenant, "platform", "graph")
    out = graph_dir(settings, tenant, mode="w")
    con = duckdb.connect()
    resolved = out.parent / "party_resolved.parquet"
    con.execute(f"CREATE VIEW resolved AS SELECT node_id, global_party_id FROM read_parquet('{resolved}')")
    counts: dict[str, int] = {}

    def copy(sql: str, path: Path, key: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        con.execute(f"COPY ({sql}) TO '{path}' (FORMAT parquet)")
        counts[key] = con.execute(f"SELECT count(*) FROM read_parquet('{path}')").fetchall()[0][0]

    for nt in o.nodes:
        files = canonical_files(settings, who, tenant, "nodes", nt.name)
        if not files:
            continue
        src = f"read_parquet({_files(files)}, union_by_name=true)"
        pii = {a.name for a in nt.attributes if a.pii}
        attrs = [a.name for a in nt.attributes if a.name not in pii]
        if nt.name == "Party":
            sql = (
                "SELECT r.global_party_id AS node_id, 'Party' AS node_type, any_value(p.subtype) AS subtype, "
                "min(p.valid_from) AS valid_from, count(DISTINCT p.source_system) AS source_cnt, "
                "list_sort(list_distinct(list(p.domain))) AS domains"
                + "".join(f", arg_min(p.{a}, p.valid_from) FILTER (p.{a} IS NOT NULL) AS {a}" for a in attrs)
                + f" FROM {src} p JOIN resolved r USING (node_id) GROUP BY r.global_party_id"
            )
        else:
            sql = (
                f"SELECT node_id, node_type, subtype, {nt.time_column}"
                + "".join(f", {a}" for a in attrs)
                + f" FROM {src} QUALIFY row_number() OVER "
                f"(PARTITION BY node_id ORDER BY {nt.time_column} NULLS LAST) = 1"
            )
        copy(sql, out / "nodes" / f"{nt.name}.parquet", f"nodes.{nt.name}")
        if nt.name == "Party":
            # every source's view of a party with the time it became known: features read attributes as of a date from this
            history = "SELECT r.global_party_id AS party_id, p.valid_from" + "".join(f", p.{a}" for a in attrs)
            copy(f"{history} FROM {src} p JOIN resolved r USING (node_id)", out / "party_history.parquet", "party_history")

    for et in o.edges:
        files = canonical_files(settings, who, tenant, "edges", et.name)
        if not files:
            continue
        sql = (
            "SELECT e.edge_type, coalesce(rs.global_party_id, e.src_id) AS src_id, "
            "coalesce(rd.global_party_id, e.dst_id) AS dst_id, e.src_type, e.dst_type, e.domain, e.source_system, "
            "e.event_time" + "".join(f", e.{a.name}" for a in et.attributes)
            + f" FROM read_parquet({_files(files)}, union_by_name=true) e "
            "LEFT JOIN resolved rs ON rs.node_id = e.src_id LEFT JOIN resolved rd ON rd.node_id = e.dst_id"
        )  # fmt: skip
        copy(sql, out / "edges" / f"{et.name}.parquet", f"edges.{et.name}")
    sizes = sorted((f.relative_to(out).as_posix(), f.stat().st_size) for f in out.glob("*/*.parquet"))
    (out / "VERSION").write_text(hashlib.sha1(json.dumps([counts, sizes]).encode()).hexdigest()[:10], encoding="utf-8")
    return counts
