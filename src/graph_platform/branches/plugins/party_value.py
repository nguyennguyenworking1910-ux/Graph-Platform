"""Node-level regression: how much a party will spend over the next months (lifetime value)."""

from __future__ import annotations

import duckdb
import numpy as np
import polars as pl
from scipy.stats import spearmanr
from sklearn.ensemble import HistGradientBoostingRegressor

from graph_platform import features
from graph_platform.branches.models import informative
from graph_platform.branches.plugins.early_warning import _shift
from graph_platform.branches.runner import Context, Result
from graph_platform.graph import graph_dir


def run(ctx: Context) -> Result:
    c = ctx.contract
    par = {"horizon_months": 6, "train_months": 6} | c.params
    par["families"] = c.params.get("families", ["degree", "khop", "community", "centrality"])
    edges = [ctx.settings.ontology_of(ctx.tenant).edge(e).name for e in c.params.get("edges", ["TRANSACTS", "PURCHASES"])]
    horizon, scope = int(par["horizon_months"]), c.graph_scope or features.load_scope(ctx.settings, ctx.tenant)
    families = ["profile", *par["families"]]
    observe = [_shift(c.as_of, -k) for k in range(horizon + int(par["train_months"]) - 1, -1, -1)]
    todo = features.missing(ctx.settings, ctx.tenant, observe, families, scope)
    if todo:
        features.build(ctx.settings, ctx.tenant, todo, families, scope)

    g = graph_dir(ctx.settings, ctx.tenant, principal="branches")
    spend = (
        duckdb.connect()
        .sql(
            " UNION ALL ".join(
                f"SELECT src_id AS party_id, event_time::DATE AS day, amount FROM read_parquet('{g / 'edges' / (e + '.parquet')}')"
                for e in edges
            )
        )
        .pl()
    )
    persons = pl.read_parquet(g / "nodes/Party.parquet").filter(pl.col("subtype") == "person").select(party_id="node_id")
    frames = []
    for d in observe:
        future = (
            spend.filter((pl.col("day") >= d) & (pl.col("day") < _shift(d, horizon)))
            .group_by("party_id")
            .agg(target=pl.col("amount").sum())
        )
        f = features.load(ctx.settings, ctx.tenant, d, families, scope).join(persons, on="party_id").join(future, on="party_id", how="left")
        frames.append(f.with_columns(pl.col("target").fill_null(0.0), as_of=pl.lit(d)))
    df = pl.concat(frames, how="diagonal_relaxed")
    test = (df["as_of"] > _shift(c.as_of, -horizon)).to_numpy()
    y = np.log1p(df["target"].to_numpy())

    out: dict = {
        "rows": {"train": int((~test).sum()), "test": int(test.sum())},
        "spenders_ratio": float((df["target"].to_numpy()[test] > 0).mean()),
    }
    scores = {}
    for name, fams in (("profile_only", ["profile"]), ("with_network", families)):
        cols = informative(df, [x for x in df.columns if x not in ("party_id", "target", "as_of") and x.split("__")[1] in fams], ~test)
        x = df.select(cols).to_numpy().astype(np.float64)
        scores[name] = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, random_state=0).fit(x[~test], y[~test]).predict(x)
        out[name] = {
            "spearman": float(spearmanr(y[test], scores[name][test]).statistic),
            "mae_log": float(np.abs(y[test] - scores[name][test]).mean()),
        }
    # share of actual future spend captured by the predicted top decile
    order = np.argsort(-scores["with_network"][test])
    actual = df["target"].to_numpy()[test][order]
    out["top_decile_value_ratio"] = float(actual[: max(actual.size // 10, 1)].sum() / max(actual.sum(), 1))
    table = (
        df.select("party_id", "as_of", "target")
        .with_columns(predicted=pl.Series(np.maximum(np.expm1(scores["with_network"]), 0.0)))
        .filter(pl.col("as_of") == c.as_of)
        .sort("predicted", descending=True)
        .with_row_index("rank", 1)
    )
    return Result(out, table)
