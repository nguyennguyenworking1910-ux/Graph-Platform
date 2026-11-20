"""Community-family detection of collusive groups: parties tied together by shared devices and internal money flow."""

from __future__ import annotations

import numpy as np
import polars as pl
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from graph_platform.branches.runner import Context, Result


def run(ctx: Context) -> Result:
    par = {"min_size": 3, "max_size": 30, "score": "internal_txn"} | ctx.contract.params
    if par["score"] not in ("internal_txn", "voucher"):
        raise ValueError(f"unknown score: {par['score']}")
    snap = ctx.snapshot(ctx.contract.as_of)
    n = snap.party_ids.size
    dev = snap.con.execute("SELECT a, b FROM peer WHERE shares_device").fetchnumpy()
    label = connected_components(coo_matrix((np.ones(dev["a"].size), (dev["a"], dev["b"])), shape=(n, n)), directed=False)[1]
    size = np.bincount(label)
    ok = (size >= par["min_size"]) & (size <= par["max_size"])
    members = pl.DataFrame({"idx": np.arange(n), "cluster": label}).filter(pl.Series(ok[label]))

    snap.con.register("members", members)
    stats = snap.con.sql(
        """SELECT ma.cluster, count(*) AS internal_txn_cnt, sum(t.amount) AS internal_amt_sum
           FROM e_TRANSACTS t
           JOIN party pa ON pa.node_id = t.src_id JOIN party pb ON pb.node_id = t.dst_id
           JOIN members ma ON ma.idx = pa.idx JOIN members mb ON mb.idx = pb.idx AND mb.cluster = ma.cluster
           GROUP BY ma.cluster"""
    ).pl()
    if par["score"] == "voucher":  # promo abuse: the group's activity is voucher redemptions, not transfers between members
        stats = snap.con.sql(
            """SELECT m.cluster, count(*) AS internal_txn_cnt, sum(p.amount) AS internal_amt_sum
               FROM e_PURCHASES p JOIN party pa ON pa.node_id = p.src_id JOIN members m ON m.idx = pa.idx
               WHERE p.voucher_code IS NOT NULL GROUP BY m.cluster"""
        ).pl()
    clusters = (
        members.group_by("cluster").len("member_cnt").join(stats, on="cluster", how="left").fill_null(0)
        # score: internal payments per member; device sharing alone (a family phone) scores zero
        .with_columns(score=(pl.col("internal_txn_cnt") / pl.col("member_cnt")).log1p())
        .sort("score", "cluster", descending=[True, False])
        .with_row_index("rank", 1)
    )  # fmt: skip
    table = members.join(clusters, on="cluster").select(
        cluster_id=pl.format("c{}", "cluster"), party_id=pl.Series(snap.party_ids)[members["idx"]], rank="rank", score="score",
        member_cnt="member_cnt", internal_txn_cnt="internal_txn_cnt", internal_amt_sum="internal_amt_sum",
    ).sort("rank", "party_id")  # fmt: skip
    metrics = {
        "cluster_cnt": clusters.height,
        "flagged_party_cnt": table.height,
        "scored_cluster_cnt": int((clusters["score"] > 0).sum()),
        "member_cnt_avg": float(clusters["member_cnt"].mean() or 0),
    }
    return Result(metrics, table)
