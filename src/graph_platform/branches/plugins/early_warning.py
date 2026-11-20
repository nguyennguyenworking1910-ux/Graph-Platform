"""Temporal family: which currently-good borrowers turn bad within the next months (default contagion, early warning)."""

from __future__ import annotations

from datetime import date

import numpy as np
import polars as pl

from graph_platform import features
from graph_platform.branches import metrics
from graph_platform.branches.models import fit_gbdt, informative
from graph_platform.branches.runner import Context, Result

LOANS = "party__profile__loan_cnt__self__all"
BAD = "party__profile__dpd90_flag__self__all"
DPD = "party__profile__dpd_max__self__all"


def _shift(d: date, months: int) -> date:
    m = d.month - 1 + months
    return date(d.year + m // 12, m % 12 + 1, 1)


def run(ctx: Context) -> Result:
    c = ctx.contract
    # min_current_dpd: only borrowers whose worst delinquency so far is at least this (collections prioritisation)
    par = {"horizon_months": 6, "train_months": 9, "min_current_dpd": 0, "families": ["khop", "community", "degree", "shared"]} | c.params
    horizon, scope = int(par["horizon_months"]), c.graph_scope or features.load_scope(ctx.settings, ctx.tenant)
    families = ["profile", *par["families"]]
    # observation months end at as_of; each needs the snapshot `horizon` months later to read its outcome
    observe = [_shift(c.as_of, -k) for k in range(horizon + int(par["train_months"]) - 1, -1, -1)]
    needed = sorted({*observe, *(_shift(d, horizon) for d in observe)})
    todo = features.missing(ctx.settings, ctx.tenant, needed, families, scope)
    if todo:
        features.build(ctx.settings, ctx.tenant, todo, families, scope)

    frames = []
    for d in observe:
        now = features.load(ctx.settings, ctx.tenant, d, families, scope).filter(
            (pl.col(LOANS) > 0) & (pl.col(BAD) == 0) & (pl.col(DPD) >= float(par["min_current_dpd"]))
        )
        later = features.load(ctx.settings, ctx.tenant, _shift(d, horizon), ["profile"], scope).select("party_id", label=pl.col(BAD))
        frames.append(now.join(later, on="party_id").with_columns(as_of=pl.lit(d)))
    df = pl.concat(frames, how="diagonal_relaxed")
    # the last `horizon` months are held out: their outcomes fall after every training outcome
    test = (df["as_of"] > _shift(c.as_of, -horizon)).to_numpy()
    y = df["label"].to_numpy().astype(int)

    out: dict = {
        "rows": {"train": int((~test).sum()), "test": int(test.sum())},
        "bad_rate": {"train": float(y[~test].mean()), "test": float(y[test].mean())},
    }
    scores = {}
    for name, fams in (("profile_only", ["profile"]), ("with_network", families)):
        cols = [x for x in df.columns if x not in ("party_id", "label", "as_of", BAD) and x.split("__")[1] in fams]
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
