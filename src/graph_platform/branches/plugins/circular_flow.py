"""Path/flow family: groups of parties circulating large, repeated transfers among themselves (layering)."""

from __future__ import annotations

import numpy as np
import polars as pl
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from graph_platform.branches.runner import Context, Result


def run(ctx: Context) -> Result:
    par = {"amount_quantile": 0.99, "min_repeat": 5, "min_size": 3, "max_size": 50} | ctx.contract.params
    snap = ctx.snapshot(ctx.contract.as_of)
    n = snap.party_ids.size
    # compute treatment: only heavy, repeated person-to-person corridors enter the flow graph
    e = snap.con.sql(
        f"""WITH p2p AS (
            SELECT pa.idx AS a, pb.idx AS b, t.amount
            FROM e_TRANSACTS t JOIN person pa ON pa.node_id = t.src_id JOIN person pb ON pb.node_id = t.dst_id)
        SELECT a, b, count(*) AS txn_cnt, sum(amount) AS amt_sum FROM p2p
        WHERE amount >= (SELECT quantile_cont(amount, {float(par["amount_quantile"])}) FROM p2p)
        GROUP BY 1, 2 HAVING count(*) >= {int(par["min_repeat"])}"""
    ).pl()
    a, b = e["a"].to_numpy(), e["b"].to_numpy()
    label = connected_components(coo_matrix((np.ones(a.size), (a, b)), shape=(n, n)), directed=True, connection="strong")[1]
    size = np.bincount(label)
    inside = (label[a] == label[b]) & (size[label[a]] >= par["min_size"]) & (size[label[a]] <= par["max_size"])
    flow = (
        e.filter(pl.Series(inside))
        .with_columns(cluster=pl.Series(label[a[inside]]))
        .group_by("cluster")
        .agg(corridor_cnt=pl.len(), internal_txn_cnt=pl.col("txn_cnt").sum(), internal_amt_sum=pl.col("amt_sum").sum())
        .sort("internal_amt_sum", "cluster", descending=[True, False])
        .with_row_index("rank", 1)
    )
    idx = np.flatnonzero(np.isin(label, flow["cluster"].to_numpy()))
    table = (
        pl.DataFrame({"party_id": snap.party_ids[idx], "cluster": label[idx]})
        .join(flow, on="cluster")
        .select(
            cluster_id=pl.format("f{}", "cluster"), party_id="party_id", rank="rank", score=pl.col("internal_amt_sum").log1p(),
            corridor_cnt="corridor_cnt", internal_txn_cnt="internal_txn_cnt", internal_amt_sum="internal_amt_sum",
        )
        .sort("rank", "party_id")
    )  # fmt: skip
    metrics = {
        "corridor_cnt": e.height,
        "cluster_cnt": flow.height,
        "flagged_party_cnt": table.height,
        "circulating_amt_sum": float(flow["internal_amt_sum"].sum()),
    }
    return Result(metrics, table)
