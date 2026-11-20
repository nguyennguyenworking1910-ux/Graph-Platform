"""Compile a mapping spec into DuckDB SQL. Only whitelisted transforms exist; no SQL comes from the spec."""

from __future__ import annotations

from graph_platform.contracts import EdgeMap, EndpointRef, Expr, MappingSpec, NodeMap, Tf
from graph_platform.contracts.mapping_spec import Attached
from graph_platform.ontology import Ontology
from graph_platform.ontology.model import SQL_TYPES
from graph_platform.trunk.landing import SourceTable, ident

CONSTANT_TIME_EDGES = ("HAS_IDENTIFIER",)  # reference links that carry no event of their own


def lit(value: object) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int | float):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def _text(x: str) -> str:
    return f"CAST({x} AS VARCHAR)"


def _tf(x: str, tf: Tf) -> str:
    a = tf.arg
    match tf.name:
        case "trim":
            return f"nullif(trim({_text(x)}), '')"
        case "lower":
            return f"lower({_text(x)})"
        case "upper":
            return f"upper({_text(x)})"
        case "cast":
            return f"TRY_CAST({x} AS {SQL_TYPES[a]})"
        case "parse_date":
            return f"CAST(try_strptime({_text(x)}, {lit(a)}) AS DATE)" if a else f"TRY_CAST({x} AS DATE)"
        case "parse_timestamp":
            return f"try_strptime({_text(x)}, {lit(a)})" if a else f"TRY_CAST({x} AS TIMESTAMP)"
        case "epoch_s":
            return f"make_timestamp(TRY_CAST({x} AS BIGINT) * 1000000)"
        case "epoch_ms":
            return f"make_timestamp(TRY_CAST({x} AS BIGINT) * 1000)"
        case "digits_only":
            return f"nullif(regexp_replace({_text(x)}, '[^0-9]', '', 'g'), '')"
        case "normalize_phone":
            d = f"regexp_replace(regexp_replace({_text(x)}, '[^0-9]', '', 'g'), '^(84|0)', '')"
            return f"CASE WHEN length({d}) BETWEEN 8 AND 11 THEN '84' || {d} END"
        case "normalize_email":
            return f"nullif(lower(trim({_text(x)})), '')"
        case "hash":
            return f"sha256({_text(x)})"
        case "map_values":
            whens = " ".join(f"WHEN {lit(k)} THEN {lit(v)}" for k, v in a.items())
            return f"CASE {_text(x)} {whens} END"
        case "scale":
            return f"TRY_CAST({x} AS DOUBLE) * {float(a)!r}"
    raise ValueError(tf.name)


def expr_sql(e: Expr) -> str:
    q = ident
    if e.const is not None:
        x = lit(e.const)
    elif e.concat is not None:
        x = "concat_ws('-', " + ", ".join(_text(q(c)) for c in e.concat) + ")"
        x = f"CASE WHEN {' AND '.join(q(c) + ' IS NOT NULL' for c in e.concat)} THEN {x} END"
    elif isinstance(e.col, list):
        x = "coalesce(" + ", ".join(q(c) for c in e.col) + ")"
    else:
        x = q(e.col)
    for tf in e.tf:
        x = _tf(x, tf)
    return x


def when_sql(m: NodeMap | EdgeMap) -> str:
    """Row filter of a node or edge mapping; values are compared as text."""
    if m.when is None:
        return "TRUE"
    return f"{_text(ident(m.when.col))} IN ({', '.join(lit(str(v).lower() if isinstance(v, bool) else str(v)) for v in m.when.is_in)})"


def node_id_sql(o: Ontology, source: str, node_type: str, subtype: str | None, key: Expr) -> str:
    k = _text(expr_sql(key))
    prefix = f"{node_type}:{subtype}:" if o.node(node_type).scope == "global" else f"{source}:{node_type}:"
    return f"{lit(prefix)} || {k}"


def _attrs(defs: list, mapped: dict[str, Expr]) -> str:
    return "".join(
        f", TRY_CAST({expr_sql(mapped[a.name]) if a.name in mapped else 'NULL'} AS {SQL_TYPES[a.type]}) AS {a.name}" for a in defs
    )


def node_sql(o: Ontology, spec: MappingSpec, table: str, m: NodeMap, run_ts: str) -> str:
    nt = o.node(m.type)
    key = expr_sql(m.key)
    time = f"CAST({expr_sql(m.time)} AS TIMESTAMP)" if m.time else "CAST(NULL AS TIMESTAMP)"
    return (
        f"SELECT {node_id_sql(o, spec.source, m.type, m.subtype, m.key)} AS node_id, "
        f"{lit(m.type)} AS node_type, {lit(m.subtype)} AS subtype, "
        f"{lit(spec.source)} AS source_system, {lit(spec.domain)} AS domain, "
        f"{_text(key)} AS source_pk, TIMESTAMP {lit(run_ts)} AS ingested_at, {time} AS {nt.time_column}"
        f"{_attrs(nt.attributes, m.attrs)} FROM {ident(table)} WHERE {key} IS NOT NULL AND {when_sql(m)}"
    )


def attached_sql(o: Ontology, spec: MappingSpec, table: str, m: Attached, personal: list[str]) -> str:
    """The table as the source has it, keyed by the party it describes; personal columns stay behind."""
    drop = f" EXCLUDE ({', '.join(ident(c) for c in personal)})" if personal else ""
    return (
        f"SELECT {node_id_sql(o, spec.source, 'Party', None, m.party)} AS node_id, "
        f"CAST({expr_sql(m.time)} AS TIMESTAMP) AS event_time, *{drop} FROM {ident(table)} WHERE {expr_sql(m.party)} IS NOT NULL"
    )


def _endpoint(o: Ontology, source: str, ref: EndpointRef) -> str:
    return node_id_sql(o, source, ref.type, ref.subtype, ref.key)


def edge_sql(o: Ontology, spec: MappingSpec, table: str, m: EdgeMap, run_ts: str) -> str:
    et = o.edge(m.type)
    pk = _text(expr_sql(m.key)) if m.key else "CAST(NULL AS VARCHAR)"
    return (
        f"SELECT {lit(m.type)} AS edge_type, {_endpoint(o, spec.source, m.src)} AS src_id, "
        f"{_endpoint(o, spec.source, m.dst)} AS dst_id, {lit(m.src.type)} AS src_type, {lit(m.dst.type)} AS dst_type, "
        f"{lit(spec.source)} AS source_system, {lit(spec.domain)} AS domain, {pk} AS source_pk, "
        f"TIMESTAMP {lit(run_ts)} AS ingested_at, CAST({expr_sql(m.time)} AS TIMESTAMP) AS event_time"
        f"{_attrs(et.attributes, m.attrs)} FROM {ident(table)} "
        f"WHERE {expr_sql(m.src.key)} IS NOT NULL AND {expr_sql(m.dst.key)} IS NOT NULL AND {when_sql(m)}"
    )


def validate(o: Ontology, spec: MappingSpec, tables: dict[str, SourceTable]) -> list[str]:
    """Static checks of a spec against the ontology and the actual source schema."""
    problems: list[str] = []

    def cols(table: str, where: str, *exprs: Expr | None) -> None:
        for e in exprs:
            for c in e.columns() if e else []:
                if c not in tables[table].columns:
                    problems.append(f"{table}.{where}: unknown column '{c}'")

    def endpoint(table: str, where: str, ref: EndpointRef, allowed: list[str]) -> None:
        if ref.type not in allowed:
            problems.append(f"{table}.{where}: {ref.type} not allowed, expected one of {allowed}")
            return
        nt = o.node(ref.type)
        if nt.scope == "global" and ref.subtype not in nt.subtypes:
            problems.append(f"{table}.{where}: global node {ref.type} needs a subtype in {nt.subtypes}")
        cols(table, where, ref.key)

    for tm in spec.tables:
        if tm.table not in tables:
            problems.append(f"unknown table '{tm.table}'")
            continue
        own = [n for n in tm.nodes if any(x.name == n.type and x.scope == "source" for x in o.nodes)]
        if own and not any(n.unique for n in tm.nodes) and not any(e.key for e in tm.edges):
            problems.append(f"{tm.table}: needs a node with unique: true or an edge with a key, so duplicates are gated")
        if tm.attached:
            cols(tm.table, "attached", tm.attached.party, tm.attached.time)
            if not tm.attached.time.columns():
                problems.append(f"{tm.table}.attached: needs a time column")
        for n in tm.nodes:
            where = f"nodes[{n.type}]"
            nt = next((x for x in o.nodes if x.name == n.type), None)
            if nt is None:
                problems.append(f"{tm.table}.{where}: unknown node type")
                continue
            if n.subtype not in nt.subtypes:
                problems.append(f"{tm.table}.{where}: subtype '{n.subtype}' not in {nt.subtypes}")
            for a in n.attrs:
                if nt.attribute(a) is None:
                    problems.append(f"{tm.table}.{where}: unknown attribute '{a}'")
            cols(tm.table, where, n.key, n.time, *n.attrs.values(), *([Expr(col=n.when.col)] if n.when else []))
        for e in tm.edges:
            where = f"edges[{e.type}]"
            et = next((x for x in o.edges if x.name == e.type), None)
            if et is None:
                problems.append(f"{tm.table}.{where}: unknown edge type")
                continue
            endpoint(tm.table, where + ".src", e.src, et.source)
            endpoint(tm.table, where + ".dst", e.dst, et.target)
            for a in e.attrs:
                if et.attribute(a) is None:
                    problems.append(f"{tm.table}.{where}: unknown attribute '{a}'")
            if not e.time.columns() and e.type not in CONSTANT_TIME_EDGES:
                problems.append(f"{tm.table}.{where}: event edges need a time column; constants are allowed only on {CONSTANT_TIME_EDGES}")
            cols(tm.table, where, e.time, e.key, *e.attrs.values(), *([Expr(col=e.when.col)] if e.when else []))
    return problems
