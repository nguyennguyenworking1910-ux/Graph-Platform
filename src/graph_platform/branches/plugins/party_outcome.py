"""Which parties will have a given outcome event within the horizon: churn, reported fraud, chargebacks, claims.

One row per (party, observation month). Label = an Event of `outcome_event` (optionally with a given status)
attached to the party in [as_of, as_of + horizon). Parties that already had the outcome are out of the population.
"""

from __future__ import annotations

import duckdb
import numpy as np
import polars as pl

from graph_platform import features
from graph_platform.branches import metrics
from graph_platform.branches.models import fit_gbdt, informative
from graph_platform.branches.plugins.early_warning import _shift
from graph_platform.branches.runner import Context, Result
from graph_platform.graph import graph_dir
from graph_platform.naming import feature_name

PRIOR = feature_name("party", "profile", "outcome_cnt", "self")


def run(ctx: Context) -> Result:
    c, o = ctx.contract, ctx.settings.ontology_of(ctx.tenant)
    # allow_prior: keep parties that already had the outcome (recurring events); their past count becomes a feature
    par = {"party_subtype": "person", "outcome_status": None, "horizon_months": 6, "train_months": 9, "allow_prior": False} | c.params
    par["families"] = c.params.get("families", ["khop", "community", "degree", "shared"])
    if par["outcome_event"] not in o.node("Event").subtypes or par["party_subtype"] not in o.node("Party").subtypes:
        raise ValueError("unknown outcome_event or party_subtype")
    horizon, scope = int(par["horizon_months"]), c.graph_scope or features.load_scope(ctx.settings, ctx.tenant)
    families = ["profile", *par["families"]]
    observe = [_shift(c.as_of, -k) for k in range(horizon + int(par["train_months"]) - 1, -1, -1)]
    todo = features.missing(ctx.settings, ctx.tenant, observe, families, scope)
    if todo:
        features.build(ctx.settings, ctx.tenant, todo, families, scope)

    g = graph_dir(ctx.settings, ctx.tenant, principal="branches")
    con = duckdb.connect()
    status = "AND e.status = $status" if par["outcome_status"] else ""
    events = con.sql(
        f"""SELECT a.src_id AS party_id, e.event_time::DATE AS day
            FROM read_parquet('{g / "nodes/Event.parquet"}') e
            JOIN read_parquet('{g / "edges/APPLIES_FOR.parquet"}') a ON a.dst_id = e.node_id
            WHERE e.subtype = $kind {status}""",
        params={"kind": par["outcome_event"], **({"status": par["outcome_status"]} if par["outcome_status"] else {})},
    ).pl()
    parties = pl.read_parquet(g / "nodes/Party.parquet").filter(pl.col("subtype") == par["party_subtype"]).select(party_id="node_id")

    frames = []
    for d in observe:
        seen = events.group_by("party_id").agg(
            pl.col("day").filter(pl.col("day") < d).len().cast(pl.Float64).alias(PRIOR),
            label=pl.col("day").filter((pl.col("day") >= d) & (pl.col("day") < _shift(d, horizon))).len() > 0,
        )
        f = features.load(ctx.settings, ctx.tenant, d, families, scope).join(parties, on="party_id").join(seen, on="party_id", how="left")
        f = f.with_columns(pl.col(PRIOR).fill_null(0), pl.col("label").fill_null(False).cast(pl.Int8), as_of=pl.lit(d))
        frames.append(f if par["allow_prior"] else f.filter(pl.col(PRIOR) == 0))
    df = pl.concat(frames, how="diagonal_relaxed")
    test = (df["as_of"] > _shift(c.as_of, -horizon)).to_numpy()
    y = df["label"].to_numpy().astype(int)

    out: dict = {
        "rows": {"train": int((~test).sum()), "test": int(test.sum())},
        "rate": {"train": float(y[~test].mean()), "test": float(y[test].mean())},
    }
    scores = {}
    for name, fams in (("profile_only", ["profile"]), ("with_network", families)):
        cols = [x for x in df.columns if x not in ("party_id", "label", "as_of") and x.split("__")[1] in fams]
        cols = informative(df, cols, ~test)
        x = df.select(cols).to_numpy().astype(np.float64)
        scores[name] = fit_gbdt(x[~test], y[~test], {}).predict_proba(x)[:, 1]
        out[name] = metrics.summary(y[test], scores[name][test])
    out["uplift"] = metrics.bootstrap_auc_delta(y[test], scores["profile_only"][test], scores["with_network"][test])
    table = (
        df.select("party_id", "as_of", "label")
        .with_columns(score=pl.Series(scores["with_network"]))
        .filter(pl.col("as_of") == c.as_of)
        .sort("score", descending=True)
        .with_row_index("rank", 1)
    )
    return Result(out, table)
