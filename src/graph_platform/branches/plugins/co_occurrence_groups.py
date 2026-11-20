"""Community family: parties that repeatedly do the same thing at the same time (co-booking groups)."""

from __future__ import annotations

import numpy as np
import polars as pl
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from graph_platform.branches.runner import Context, Result


def run(ctx: Context) -> Result:
    o = ctx.settings.ontology_of(ctx.tenant)
    par = {"edge": "PURCHASES", "item_subtype": "trip", "window_minutes": 60, "min_together": 2, "max_size": 20} | ctx.contract.params
    edge = o.edge(par["edge"]).name
    if par["item_subtype"] not in o.node("Item").subtypes:
        raise ValueError(f"unknown item subtype: {par['item_subtype']}")
    snap = ctx.snapshot(ctx.contract.as_of)
    n = snap.party_ids.size
    pairs = snap.con.sql(
        f"""WITH buy AS (
            SELECT p.idx, e.dst_id AS item, e.event_time FROM e_{edge} e JOIN party p ON p.node_id = e.src_id
            JOIN n_Item i ON i.node_id = e.dst_id AND i.subtype = $subtype)
        SELECT x.idx AS a, y.idx AS b, count(*) AS together_cnt
        FROM buy x JOIN buy y ON x.item = y.item AND x.idx < y.idx
             AND abs(epoch(x.event_time) - epoch(y.event_time)) <= {int(par["window_minutes"])} * 60
        GROUP BY 1, 2 HAVING count(*) >= {int(par["min_together"])}""",
        params={"subtype": par["item_subtype"]},
    ).pl()
    a, b = pairs["a"].to_numpy(), pairs["b"].to_numpy()
    label = connected_components(coo_matrix((np.ones(a.size), (a, b)), shape=(n, n)), directed=False)[1]
    size = np.bincount(label)
    ok = (size >= 2) & (size <= int(par["max_size"]))
    together = np.bincount(label[a], weights=pairs["together_cnt"].to_numpy().astype(float), minlength=size.size)
    idx = np.flatnonzero(ok[label])
    groups = (
        pl.DataFrame({"cluster": np.flatnonzero(ok), "member_cnt": size[ok], "together_cnt": together[ok]})
        .sort("together_cnt", "cluster", descending=[True, False])
        .with_row_index("rank", 1)
    )
    table = (
        pl.DataFrame({"party_id": snap.party_ids[idx], "cluster": label[idx]})
        .join(groups, on="cluster")
        .select(
            cluster_id=pl.format("g{}", "cluster"),
            party_id="party_id",
            rank="rank",
            score=pl.col("together_cnt").log1p(),
            member_cnt="member_cnt",
        )
        .sort("rank", "party_id")
    )
    metrics = {
        "pair_cnt": pairs.height,
        "group_cnt": groups.height,
        "grouped_party_cnt": table.height,
        "member_cnt_avg": float(groups["member_cnt"].mean() or 0),
    }
    return Result(metrics, table)
