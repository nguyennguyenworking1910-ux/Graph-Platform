"""Data dictionary drafting from a profile, and linking columns to their ontology targets."""

from __future__ import annotations

import re

from graph_platform.contracts import ColumnDict, DataDictionary, Expr, MappingSpec, TableDict

PII = {"name", "phone", "email", "national_id", "address"}
HINTS = [  # (semantic type, column-name pattern), first match wins
    ("national_id", r"(^|_)(nid|kyc|citizen|national|id_card)"),
    ("device_id", r"device|(^|_)dev(_|$)"),
    ("ip", r"(^|_)ip(_|$)"),
    ("currency", r"(^|_)(ccy|currency)"),
    ("status", r"status|decision|state"),
    ("geo", r"city|country|destination|province"),
    ("name", r"name|(^|_)nm(_|$)|nickname"),
    ("amount", r"amount|amt|price|premium|principal|income|balance|value"),
    ("flag", r"^(is|has)_"),
]


def _semantic(col: dict, is_fk: bool, rows: int) -> str:
    p, name = col["patterns"], col["name"].lower()
    non_null = rows * (1 - col["null_rate"])
    near_unique = non_null > 0 and col["distinct"] / non_null >= 0.95
    if p["email"] >= 0.8:
        return "email"
    hinted = next((s for s, rx in HINTS if re.search(rx, name)), None)
    if hinted in ("national_id", "device_id"):
        return hinted
    if p["epoch_ms"] >= 0.8 or col["dtype"].startswith("TIMESTAMP"):
        return "timestamp"
    if p["date"] >= 0.8:
        return "timestamp" if ":" in (col["max"] or "") else "date"
    if p["phone"] >= 0.8 and p["numeric"] < 0.9:
        return "phone"
    if col["dtype"] == "BOOLEAN":
        return "flag"
    if is_fk:
        return "foreign_key"
    if near_unique and p["date"] < 0.8:
        return "id"
    if hinted:
        return hinted
    if p["numeric"] >= 0.99:
        return "numeric"
    return "category" if col["low_cardinality"] else "text"


def draft(profile: dict) -> DataDictionary:
    """Deterministic first draft; descriptions are refined by the enterprise or the profiler agent."""
    fks = {(f["table"], f["column"]): f["references"] for f in profile["foreign_keys"]}
    tables = []
    for t in profile["tables"]:
        cols = []
        for c in t["columns"]:
            ref = fks.get((t["name"], c["name"]))
            sem = _semantic(c, ref is not None, t["rows"])
            cols.append(
                ColumnDict(
                    name=c["name"],
                    dtype=c["dtype"],
                    semantic_type=sem,
                    description=f"{sem.replace('_', ' ')} of {t['name']}" + (f", references {ref}" if ref else ""),
                    pii=sem in PII,
                    references=ref,
                )
            )
        pk = [c.name for c in cols if c.semantic_type == "id"][:1]
        tables.append(TableDict(name=t["name"], description=f"{t['name']} table of {profile['source']}", primary_key=pk, columns=cols))
    return DataDictionary(source=profile["source"], domain=profile["domain"], tables=tables)


def coverage_problems(d: DataDictionary, columns: dict[str, dict[str, str]]) -> list[str]:
    """Every source column must be described."""
    out = []
    for table, cols in columns.items():
        td = d.table(table)
        if td is None:
            out.append(f"dictionary: table '{table}' missing")
            continue
        out += [f"dictionary: column '{table}.{c}' missing" for c in cols if td.column(c) is None]
    return out


def annotate(d: DataDictionary, spec: MappingSpec) -> DataDictionary:
    """Record on each column where the mapping spec sends it."""
    targets: dict[tuple[str, str], str] = {}

    def put(table: str, e: Expr | None, target: str) -> None:
        for c in e.columns() if e else []:
            targets.setdefault((table, c), target)

    for tm in spec.tables:
        for n in tm.nodes:
            put(tm.table, n.key, f"{n.type}.key")
            put(tm.table, n.time, f"{n.type}.time")
            for a, e in n.attrs.items():
                put(tm.table, e, f"{n.type}.{a}")
        for e in tm.edges:
            put(tm.table, e.time, f"{e.type}.event_time")
            for a, x in e.attrs.items():
                put(tm.table, x, f"{e.type}.{a}")
    out = d.model_copy(deep=True)
    for t in out.tables:
        for c in t.columns:
            c.maps_to = targets.get((t.name, c.name))
    return out
