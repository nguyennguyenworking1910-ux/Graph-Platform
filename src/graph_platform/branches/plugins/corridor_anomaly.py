"""Path/flow family: cross-border corridors whose volume suddenly departs from their own history."""

from __future__ import annotations

from graph_platform.branches.runner import Context, Result


def run(ctx: Context) -> Result:
    par = {"recent_days": 90, "min_recent_txn": 30} | ctx.contract.params
    days = int(par["recent_days"])
    snap = ctx.snapshot(ctx.contract.as_of)
    cut = f"TIMESTAMP '{ctx.contract.as_of} 00:00:00' - INTERVAL {days} DAY"
    table = snap.con.sql(
        f"""WITH t AS (
            SELECT corridor, src_id, amount, event_time >= {cut} AS recent, event_time FROM e_TRANSACTS WHERE corridor IS NOT NULL),
        span AS (SELECT greatest(date_diff('day', min(event_time), {cut}), {days}) / {days}.0 AS periods FROM t)
        SELECT corridor,
               count(*) FILTER (recent) AS recent_txn_cnt,
               round(count(*) FILTER (NOT recent) / (SELECT periods FROM span), 1) AS usual_txn_cnt,
               count(DISTINCT src_id) FILTER (recent) AS sender_nuniq,
               coalesce(sum(amount) FILTER (recent), 0) AS recent_amt_sum,
               round((count(*) FILTER (recent) + 1) / (count(*) FILTER (NOT recent) / (SELECT periods FROM span) + 1), 2) AS score
        FROM t GROUP BY corridor HAVING count(*) FILTER (recent) >= {int(par["min_recent_txn"])}
        ORDER BY score DESC, corridor"""
    ).pl().with_row_index("rank", 1)  # fmt: skip
    metrics = {"corridor_cnt": table.height, "growth_max": float(table["score"].max() or 0)}
    return Result(metrics, table)
