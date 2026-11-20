"""Edge-level family: classify single edges (a transfer that will be disputed, a fraudulent payment).

Features of both endpoints are taken at as_of; edges are the ones that happen after it: the first
`train_days` train the model, the following `test_days` evaluate it.
"""

from __future__ import annotations

from datetime import timedelta

import duckdb
import numpy as np
import polars as pl

from graph_platform import features
from graph_platform.branches import metrics
from graph_platform.branches.models import fit_gbdt, informative
from graph_platform.branches.runner import Context, Result
from graph_platform.graph import graph_dir


def run(ctx: Context) -> Result:
    c, o = ctx.contract, ctx.settings.ontology_of(ctx.tenant)
    par = {"edge": "TRANSACTS", "label_attribute": "status", "label_value": "disputed", "train_days": 120, "test_days": 60} | c.params
    par["families"] = c.params.get("families", ["profile", "degree", "khop", "shared", "community"])
    edge = o.edge(par["edge"])
    if edge.attribute(par["label_attribute"]) is None:
        raise ValueError(f"{edge.name} has no attribute {par['label_attribute']}")
    scope = c.graph_scope or features.load_scope(ctx.settings, ctx.tenant)
    if features.missing(ctx.settings, ctx.tenant, [c.as_of], par["families"], scope):
        features.build(ctx.settings, ctx.tenant, [c.as_of], par["families"], scope)
    feats = features.load(ctx.settings, ctx.tenant, c.as_of, par["families"], scope)

    split, end = c.as_of + timedelta(days=int(par["train_days"])), c.as_of + timedelta(days=int(par["train_days"]) + int(par["test_days"]))
    g = graph_dir(ctx.settings, ctx.tenant, principal="branches")
    edges = (
        duckdb.connect()
        .sql(
            f"""SELECT src_id, dst_id, event_time, amount, coalesce({par["label_attribute"]} = $value, FALSE)::INT AS label,
                   event_time >= DATE '{split}' AS test
            FROM read_parquet('{g / "edges" / (edge.name + ".parquet")}')
            WHERE src_type = 'Party' AND dst_type = 'Party' AND event_time >= DATE '{c.as_of}' AND event_time < DATE '{end}'""",
            params={"value": par["label_value"]},
        )
        .pl()
    )
    # how often the pair interacted before as_of: a first-time counterparty is the classic fraud signal
    snap = ctx.snapshot(c.as_of)
    pair = snap.con.sql(
        "SELECT pa.node_id AS src_id, pb.node_id AS dst_id, r.w AS pair_interaction_cnt FROM peer r "
        "JOIN party pa ON pa.idx = r.a JOIN party pb ON pb.idx = r.b"
    ).pl()
    pair = pl.concat([pair, pair.select(src_id="dst_id", dst_id="src_id", pair_interaction_cnt="pair_interaction_cnt")])
    names = feats.columns[1:]
    df = (
        edges.join(feats.rename({n: f"src__{n}" for n in names}), left_on="src_id", right_on="party_id", how="left")
        .join(feats.rename({n: f"dst__{n}" for n in names}), left_on="dst_id", right_on="party_id", how="left")
        .join(pair, on=["src_id", "dst_id"], how="left")
        .with_columns(pl.col("pair_interaction_cnt").fill_null(0))
    )
    test, y = df["test"].to_numpy(), df["label"].to_numpy()
    out: dict = {
        "rows": {"train": int((~test).sum()), "test": int(test.sum())},
        "rate": {"train": float(y[~test].mean()), "test": float(y[test].mean())},
    }
    scores = {}
    for name, cols in (
        ("amount_only", ["amount"]),
        ("with_network", ["amount", "pair_interaction_cnt", *[x for x in df.columns if x[:5] in ("src__", "dst__")]]),
    ):
        cols = informative(df, cols, ~test)
        x = df.select(cols).to_numpy().astype(np.float64)
        scores[name] = fit_gbdt(x[~test], y[~test], {}).predict_proba(x)[:, 1]
        out[name] = metrics.summary(y[test], scores[name][test])
    out["uplift"] = metrics.bootstrap_auc_delta(y[test], scores["amount_only"][test], scores["with_network"][test], n=100)
    table = (
        df.filter(pl.col("test"))
        .select("src_id", "dst_id", "event_time", "amount", "label", score=pl.Series(scores["with_network"][test]))
        .sort("score", descending=True)
        .with_row_index("rank", 1)
    )
    return Result(out, table)
