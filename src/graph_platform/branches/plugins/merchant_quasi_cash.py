"""Merchants used to turn balance into cash: few payers, large round amounts, repeated."""

from __future__ import annotations

import polars as pl

from graph_platform.branches.runner import Context, Result


def run(ctx: Context) -> Result:
    par = {"round_unit": 1_000_000, "top_payers": 5, "min_txn": 20} | ctx.contract.params
    snap = ctx.snapshot(ctx.contract.as_of)
    table = snap.con.sql(
        f"""WITH pay AS (
            SELECT t.dst_id AS party_id, t.src_id AS payer, t.amount
            FROM e_TRANSACTS t JOIN party m ON m.node_id = t.dst_id AND m.subtype = 'merchant'
            JOIN person p ON p.node_id = t.src_id),
        by_payer AS (
            SELECT party_id, payer, sum(amount) AS amt,
                   row_number() OVER (PARTITION BY party_id ORDER BY sum(amount) DESC, payer) AS r
            FROM pay GROUP BY party_id, payer),
        top AS (SELECT party_id, sum(amt) FILTER (r <= {int(par["top_payers"])}) / sum(amt) AS top_payer_ratio FROM by_payer GROUP BY 1),
        agg AS (
            SELECT party_id, count(*) AS txn_cnt, count(DISTINCT payer) AS payer_nuniq, avg(amount) AS amt_avg, sum(amount) AS amt_sum,
                   avg((amount % {float(par["round_unit"])} = 0)::INT) AS round_amt_ratio
            FROM pay GROUP BY 1 HAVING count(*) >= {int(par["min_txn"])})
        SELECT a.*, t.top_payer_ratio, 1 - a.payer_nuniq / a.txn_cnt AS repeat_ratio,
               a.round_amt_ratio * t.top_payer_ratio * (1 - a.payer_nuniq / a.txn_cnt) AS score
        FROM agg a JOIN top t USING (party_id)
        ORDER BY score DESC, party_id"""
    ).pl().with_row_index("rank", 1)  # fmt: skip
    metrics = {
        "merchant_cnt": table.height,
        "flagged_cnt": int((table["score"] > 0.5).sum()),
        "flagged_amt_sum": float(table.filter(pl.col("score") > 0.5)["amt_sum"].sum()),
    }
    return Result(metrics, table)
