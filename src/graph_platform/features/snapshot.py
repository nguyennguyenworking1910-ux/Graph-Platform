"""Point-in-time view of the graph. Every feature reads through this and nothing else."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb
import numpy as np
from scipy.sparse import csr_matrix

from graph_platform.contracts import GraphScope
from graph_platform.ontology import Ontology
from graph_platform.ontology.model import SQL_TYPES


@dataclass
class Snapshot:
    con: duckdb.DuckDBPyConnection
    as_of: date
    party_ids: np.ndarray  # index -> party node_id (persons and merchants known before as_of)
    peers: csr_matrix  # symmetric adjacency between parties of the same subtype, weight = interaction count
    party_extra: tuple[tuple[str, str], ...] = ()  # (name, type) of the tenant's own non-PII party attributes


def open_snapshot(graph_dir: Path, as_of: date, ontology: Ontology, scope: GraphScope) -> Snapshot:
    """Views n_<Type> / e_<TYPE> hold only rows strictly before as_of. Types a tenant lacks are empty views."""
    con = duckdb.connect()
    cut = f"TIMESTAMP '{as_of.isoformat()} 00:00:00'"

    def missing(path: Path, attributes: list) -> str:
        cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{path}')").fetchall()}
        return "".join(f", NULL::{SQL_TYPES[a.type]} AS {a.name}" for a in attributes if a.name not in cols)

    for f in sorted((graph_dir / "nodes").glob("*.parquet")):
        nt = ontology.node(f.stem)
        # nodes without a recorded time (reference data) are treated as always known
        con.execute(
            f"CREATE VIEW n_{f.stem} AS SELECT *{missing(f, nt.attributes)} FROM read_parquet('{f}') "
            f"WHERE {nt.time_column} < {cut} OR {nt.time_column} IS NULL"
        )
    for f in sorted((graph_dir / "edges").glob("*.parquet")):
        con.execute(
            f"CREATE VIEW e_{f.stem} AS SELECT *{missing(f, ontology.edge(f.stem).attributes)} "
            f"FROM read_parquet('{f}') WHERE event_time < {cut}"
            + (f" AND event_time >= {cut} - INTERVAL {scope.lookback_days} DAY" if f.stem in scope.windowed_edges else "")
        )

    have = {r[0] for r in con.execute("SELECT view_name FROM duckdb_views()").fetchall()}
    for nt in ontology.nodes:
        if f"n_{nt.name}" not in have:
            cols = "".join(f", NULL::{SQL_TYPES[a.type]} AS {a.name}" for a in nt.attributes)
            con.execute(
                f"CREATE VIEW n_{nt.name} AS SELECT NULL::VARCHAR AS node_id, NULL::VARCHAR AS subtype, "
                f"NULL::TIMESTAMP AS {nt.time_column}{cols} WHERE FALSE"
            )
    for et in ontology.edges:
        if f"e_{et.name}" not in have:
            cols = "".join(f", NULL::{SQL_TYPES[a.type]} AS {a.name}" for a in et.attributes)
            con.execute(
                f"CREATE VIEW e_{et.name} AS SELECT NULL::VARCHAR AS src_id, NULL::VARCHAR AS dst_id, "
                f"NULL::TIMESTAMP AS event_time{cols} WHERE FALSE"
            )

    # party attributes as known at as_of: the earliest non-null value among source records that existed by then
    history = graph_dir / "party_history.parquet"
    known = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{history}')").fetchall()} - {"party_id", "valid_from"}
    attrs = [a for a in ontology.node("Party").attributes if not a.pii]
    picks = "".join(
        f", arg_min({a.name}, valid_from) FILTER ({a.name} IS NOT NULL) AS {a.name}"
        if a.name in known
        else f", NULL::{SQL_TYPES[a.type]} AS {a.name}"
        for a in attrs
    )
    con.execute(
        f"""CREATE TABLE party AS
        SELECT row_number() OVER (ORDER BY n.node_id) - 1 AS idx, n.* EXCLUDE ({", ".join(a.name for a in attrs)}), h.* EXCLUDE (party_id)
        FROM n_Party n LEFT JOIN (
            SELECT party_id{picks} FROM read_parquet('{history}') WHERE valid_from < {cut} OR valid_from IS NULL GROUP BY party_id
        ) h ON h.party_id = n.node_id"""
    )
    # built in materialized steps: as one statement the planner picks pathological join orders on some dates
    k = scope.max_degree
    for sql in (
        "CREATE TABLE person AS SELECT node_id, idx FROM party WHERE subtype = 'person'",
        "CREATE TABLE peerable AS SELECT node_id, idx, subtype FROM party",
        "CREATE TABLE dev AS SELECT dst_id AS device, src_id AS party FROM e_USES GROUP BY ALL",
        f"CREATE TABLE dev_ok AS SELECT device FROM dev GROUP BY device HAVING count(*) BETWEEN 2 AND {scope.device_fanout}",
        """CREATE TABLE dev_pair AS
            SELECT x.party AS a, y.party AS b FROM dev x JOIN dev y ON x.device = y.device AND x.party < y.party
            WHERE x.device IN (SELECT device FROM dev_ok)""",
        """CREATE TABLE pair AS
            SELECT src_id AS a, dst_id AS b, 'transacts' AS rel FROM e_TRANSACTS
            UNION ALL SELECT src_id, dst_id, 'refers' FROM e_REFERS
            UNION ALL SELECT a, b, 'device' FROM dev_pair""",
        # endpoints are resolved to indexes first; filtering on subtype inside the join made the planner blow up
        """CREATE TABLE pair_idx AS
            SELECT pa.idx AS ia, pb.idx AS ib, pa.subtype AS sa, pb.subtype AS sb, p.rel
            FROM pair p JOIN peerable pa ON pa.node_id = p.a JOIN peerable pb ON pb.node_id = p.b""",
        f"""CREATE TABLE agg AS
            SELECT least(ia, ib) AS a, greatest(ia, ib) AS b, count(*) AS w,
                   count(*) FILTER (rel = 'device') > 0 AS shares_device
            FROM pair_idx WHERE sa = sb AND ia <> ib GROUP BY 1, 2
            HAVING count(*) >= {scope.min_weight} OR count(*) FILTER (rel <> 'transacts') > 0""",
        """CREATE TABLE ranked AS
            SELECT n, o, row_number() OVER (PARTITION BY n ORDER BY w DESC, o) AS r
            FROM (SELECT a AS n, b AS o, w FROM agg UNION ALL SELECT b, a, w FROM agg)""",
        f"""CREATE TABLE peer AS
            SELECT g.* FROM agg g
            JOIN (SELECT n, o FROM ranked WHERE r <= {k}) ra ON ra.n = g.a AND ra.o = g.b
            JOIN (SELECT n, o FROM ranked WHERE r <= {k}) rb ON rb.n = g.b AND rb.o = g.a""",
    ):
        con.execute(sql)
    ids = con.execute("SELECT node_id FROM party ORDER BY idx").fetchnumpy()["node_id"]
    e = con.execute("SELECT a, b, w FROM peer").fetchnumpy()
    n = ids.size
    adj = csr_matrix((np.r_[e["w"], e["w"]].astype(np.float64), (np.r_[e["a"], e["b"]], np.r_[e["b"], e["a"]])), shape=(n, n))
    return Snapshot(con, as_of, ids, adj, tuple((a.name, a.type) for a in attrs if a.extension))
