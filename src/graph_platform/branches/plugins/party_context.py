"""Knowledge family: a compact, PII-free context document per party, ready for question answering and explanations."""

from __future__ import annotations

import json

import polars as pl

from graph_platform import features
from graph_platform.branches.runner import Context, Result


def run(ctx: Context) -> Result:
    c = ctx.contract
    par = {"limit": 1000, "peers": 10, "families": ["profile", "degree", "khop", "shared", "community"]} | c.params
    scope = c.graph_scope or features.load_scope(ctx.settings, ctx.tenant)
    if features.missing(ctx.settings, ctx.tenant, [c.as_of], par["families"], scope):
        features.build(ctx.settings, ctx.tenant, [c.as_of], par["families"], scope)
    feats = features.load(ctx.settings, ctx.tenant, c.as_of, par["families"], scope)
    snap = ctx.snapshot(c.as_of)
    # the parties most worth explaining first: those with the most relationships
    chosen = snap.con.sql(f"SELECT n AS idx, count(*) AS peer_cnt FROM ranked GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT {int(par['limit'])}").pl()
    snap.con.register("chosen", chosen)
    peer = "{'peer': q.node_id, 'interactions': r.w, 'shares_device': r.shares_device}"
    peers = snap.con.sql(
        f"""SELECT p.node_id AS party_id,
                   list({peer} ORDER BY r.w DESC)[:{int(par["peers"])}] AS peers
            FROM chosen c JOIN party p ON p.idx = c.idx
            JOIN (SELECT a AS n, b AS o, w, shares_device FROM peer UNION ALL SELECT b, a, w, shares_device FROM peer) r ON r.n = c.idx
            JOIN party q ON q.idx = r.o GROUP BY 1"""
    ).pl()
    docs = peers.join(feats, on="party_id")
    feature_cols = [x for x in feats.columns if x != "party_id"]
    table = docs.select(
        "party_id",
        context=pl.struct(["peers", *feature_cols]).map_elements(
            lambda r: json.dumps(
                {"as_of": str(c.as_of), "features": {k: r[k] for k in feature_cols if r[k] is not None}, "peers": r["peers"]}
            ),
            return_dtype=pl.String,
        ),
    )
    metrics = {"document_cnt": table.height, "context_chars_avg": float(table["context"].str.len_chars().mean() or 0)}
    return Result(metrics, table)
