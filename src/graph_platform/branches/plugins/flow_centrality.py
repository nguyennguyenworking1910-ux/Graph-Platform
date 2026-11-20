"""Path/flow family: who sits at the centre of a directed flow (referral influence, supply-chain hubs)."""

from __future__ import annotations

import numpy as np
import polars as pl
from scipy.sparse import coo_matrix, diags

from graph_platform.branches.runner import Context, Result


def run(ctx: Context) -> Result:
    o = ctx.settings.ontology_of(ctx.tenant)
    par = {"edge": "TRANSACTS", "party_subtype": "merchant", "weight": "amount", "relation": None} | ctx.contract.params
    edge = o.edge(par["edge"]).name
    if par["party_subtype"] not in o.node("Party").subtypes or par["weight"] not in ("amount", "count"):
        raise ValueError("unknown party_subtype or weight")
    snap = ctx.snapshot(ctx.contract.as_of)
    n = snap.party_ids.size
    weight = "sum(e.amount)" if par["weight"] == "amount" else "count(*)"
    relation = "AND e.relation = $relation" if par["relation"] else ""
    e = snap.con.sql(
        f"""SELECT pa.idx AS a, pb.idx AS b, {weight} AS w
            FROM e_{edge} e JOIN party pa ON pa.node_id = e.src_id AND pa.subtype = $subtype
            JOIN party pb ON pb.node_id = e.dst_id AND pb.subtype = $subtype
            WHERE pa.idx <> pb.idx {relation} GROUP BY 1, 2""",
        params={"subtype": par["party_subtype"], **({"relation": par["relation"]} if par["relation"] else {})},
    ).pl()
    a, b, w = e["a"].to_numpy(), e["b"].to_numpy(), e["w"].to_numpy().astype(float)
    flow = coo_matrix((w, (a, b)), shape=(n, n)).tocsr()
    out_total = np.asarray(flow.sum(axis=1)).ravel()
    walk = diags(np.divide(1.0, out_total, out=np.zeros(n), where=out_total > 0)) @ flow
    active = np.zeros(n, bool)
    active[np.r_[a, b]] = True
    rank = active / max(active.sum(), 1)
    for _ in range(50):  # PageRank over the flow: weight arrives where the flow ends up
        rank = active * (0.15 / max(active.sum(), 1)) + 0.85 * (walk.T @ rank)
    reach = np.asarray((walk > 0).sum(axis=0)).ravel()
    table = (
        pl.DataFrame(
            {
                "party_id": snap.party_ids[active],
                "score": rank[active] * active.sum(),
                "inflow_sum": np.asarray(flow.sum(axis=0)).ravel()[active],
                "outflow_sum": out_total[active],
                "source_cnt": reach[active],
            }
        )
        .sort("score", "party_id", descending=[True, False])
        .with_row_index("rank", 1)
    )
    metrics = {
        "party_cnt": table.height,
        "edge_cnt": e.height,
        "top10_inflow_ratio": float(table.head(10)["inflow_sum"].sum() / max(w.sum(), 1)),
    }
    return Result(metrics, table)
