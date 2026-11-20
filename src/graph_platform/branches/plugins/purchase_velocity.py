"""Node-level anomaly: parties buying the same item many times in a short window (scalping, bots)."""

from __future__ import annotations

from graph_platform.branches.runner import Context, Result


def run(ctx: Context) -> Result:
    o = ctx.settings.ontology_of(ctx.tenant)
    par = {"edge": "PURCHASES", "item_subtype": "movie", "window_hours": 2, "min_purchases": 5} | ctx.contract.params
    edge = o.edge(par["edge"]).name
    if par["item_subtype"] not in o.node("Item").subtypes:
        raise ValueError(f"unknown item subtype: {par['item_subtype']}")
    snap = ctx.snapshot(ctx.contract.as_of)
    table = snap.con.sql(
        f"""WITH buy AS (
            SELECT e.src_id AS party_id, e.dst_id AS item, coalesce(e.quantity, 1) AS quantity,
                   time_bucket(INTERVAL {int(par["window_hours"])} HOUR, e.event_time) AS bucket
            FROM e_{edge} e JOIN n_Item i ON i.node_id = e.dst_id AND i.subtype = $subtype),
        burst AS (SELECT party_id, item, bucket, count(*) AS purchase_cnt, sum(quantity) AS quantity_sum FROM buy GROUP BY ALL)
        SELECT party_id, max(purchase_cnt) AS burst_purchase_max, max(quantity_sum) AS burst_quantity_max,
               count(*) FILTER (purchase_cnt >= {int(par["min_purchases"])}) AS burst_cnt,
               max(purchase_cnt)::DOUBLE AS score
        FROM burst GROUP BY 1 HAVING max(purchase_cnt) >= {int(par["min_purchases"])}
        ORDER BY score DESC, burst_quantity_max DESC, party_id""",
        params={"subtype": par["item_subtype"]},
    ).pl().with_row_index("rank", 1)  # fmt: skip
    return Result({"flagged_cnt": table.height, "burst_purchase_max": float(table["burst_purchase_max"].max() or 0)}, table)
