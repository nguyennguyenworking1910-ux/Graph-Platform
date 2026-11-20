"""Deterministic profiling. The agent view carries statistics and masked samples only."""

from __future__ import annotations

import re

import duckdb

from graph_platform.trunk.landing import SourceTable, connect, ident

PATTERNS = {
    "phone": r"^\(?\+?[0-9][0-9 .()\-]{7,16}$",
    "email": r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$",
    "date": r"^(\d{4}[-/]\d{2}[-/]\d{2}|\d{2}/\d{2}/\d{4})([ T]\d{2}:\d{2}:\d{2})?$",
    "numeric": r"^-?\d+(\.\d+)?$",
    "epoch_ms": r"^1[5-9]\d{11}$",
}
SAMPLE = 20_000


def mask(value: object) -> str | None:
    """Keep the shape of a value, drop its content."""
    if value is None:
        return None
    return re.sub(r"[a-z]", "a", re.sub(r"[A-Z]", "A", re.sub(r"\d", "9", str(value))))


def _column(con: duckdb.DuckDBPyConnection, table: SourceTable, col: str) -> dict:
    q, tbl = ident(col), ident(table.name)
    rows, nulls, distinct, lo, hi = con.execute(
        f"SELECT count(*), count(*) - count({q}), count(DISTINCT {q}), min({q})::VARCHAR, max({q})::VARCHAR FROM {tbl}"
    ).fetchone()
    sample = f"(SELECT {q}::VARCHAR v FROM {tbl} WHERE {q} IS NOT NULL LIMIT {SAMPLE})"
    rates = con.execute(
        "SELECT " + ", ".join(f"avg(regexp_matches(v, '{p}')::INT)" for p in PATTERNS.values()) + f" FROM {sample}"
    ).fetchone()
    top = con.execute(f"SELECT v, count(*) c FROM {sample} GROUP BY v ORDER BY c DESC, v LIMIT 5").fetchall()
    non_null = rows - nulls
    return {
        "name": col,
        "dtype": table.columns[col],
        "null_rate": round(nulls / rows, 4) if rows else 0.0,
        "distinct": distinct,
        "unique": non_null > 0 and distinct == non_null,
        "min": lo,
        "max": hi,
        "patterns": {k: round(v or 0.0, 3) for k, v in zip(PATTERNS, rates, strict=True)},
        "top_values": [v for v, _ in top],
        "low_cardinality": distinct <= 50,
    }


def _foreign_keys(con: duckdb.DuckDBPyConnection, profile: dict) -> list[dict]:
    """Column A references key B when (almost) every sampled value of A exists in B."""
    keys = [(t["name"], c["name"]) for t in profile["tables"] for c in t["columns"] if c["unique"]]
    out = []
    for t in profile["tables"]:
        for c in t["columns"]:
            if c["patterns"]["numeric"] == 1 and c["low_cardinality"]:
                continue
            for kt, kc in keys:
                if kt == t["name"]:
                    continue
                hit = con.execute(
                    f"SELECT avg((v IN (SELECT {ident(kc)}::VARCHAR FROM {ident(kt)}))::INT) FROM "
                    f"(SELECT DISTINCT {ident(c['name'])}::VARCHAR v FROM {ident(t['name'])} "
                    f"WHERE {ident(c['name'])} IS NOT NULL LIMIT 2000)"
                ).fetchone()[0]
                if hit is not None and hit >= 0.98:
                    out.append({"table": t["name"], "column": c["name"], "references": f"{kt}.{kc}", "coverage": hit})
    return out


def profile_source(source: str, domain: str, tables: dict[str, SourceTable]) -> dict:
    con = connect(tables)
    profile = {
        "source": source,
        "domain": domain,
        "tables": [
            {
                "name": t.name,
                "rows": t.rows,
                "fingerprint": t.fingerprint,
                "columns": [_column(con, t, c) for c in t.columns],
            }
            for t in tables.values()
        ],
    }
    profile["foreign_keys"] = _foreign_keys(con, profile)
    return profile


def agent_view(profile: dict) -> dict:
    """Profile safe to send to an LLM: every literal value is masked unless the column is a low-cardinality code."""
    out = {**profile, "tables": []}
    for t in profile["tables"]:
        cols = []
        for c in t["columns"]:
            keep = c["low_cardinality"] and c["patterns"]["phone"] < 0.5 and c["patterns"]["email"] < 0.5
            cols.append(
                {
                    **c,
                    "min": mask(c["min"]),
                    "max": mask(c["max"]),
                    "top_values": c["top_values"] if keep else [mask(v) for v in c["top_values"]],
                }
            )
        out["tables"].append({**t, "columns": cols})
    return out
