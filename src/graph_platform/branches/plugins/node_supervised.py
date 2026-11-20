"""Node-level supervised scoring: credit score and every other `node_level` problem, driven by the experiment config."""

from __future__ import annotations

import numpy as np
import polars as pl
from sklearn.inspection import permutation_importance

from graph_platform.branches import metrics
from graph_platform.branches.dataset import KEYS, feature_columns, scope_of, training_table
from graph_platform.branches.models import fit_gbdt, graphsage_scores, informative
from graph_platform.branches.runner import Context, Result

EVAL_SPLITS = ("test", "oot")


def run(ctx: Context) -> Result:
    c = ctx.contract
    df = training_table(ctx.settings, ctx.tenant, c)
    y = df["label"].to_numpy()
    mask = {s: (df["split"] == s).to_numpy() for s in ("train", *EVAL_SPLITS)}

    scores: dict[str, np.ndarray] = {}
    fitted: dict[str, object] = {}
    importance: dict[str, list[dict]] = {}
    for spec in c.models:
        if spec.kind == "graphsage":
            event_cols = [x for x in feature_columns(df, ["profile"]) if x.endswith("__application__all")]
            scores[spec.name], fitted[spec.name] = graphsage_scores(
                ctx.settings, ctx.tenant, df, spec, scope_of(ctx.settings, ctx.tenant, c), event_cols
            )
            continue
        cols = informative(df, feature_columns(df, spec.families), mask["train"])
        x = df.select(cols).to_numpy().astype(np.float64)
        model = fit_gbdt(x[mask["train"]], y[mask["train"]], spec.params)
        scores[spec.name], fitted[spec.name] = model.predict_proba(x)[:, 1], model
        xo, yo = x[mask["oot"]], y[mask["oot"]]
        pick = np.random.default_rng(0).choice(xo.shape[0], min(xo.shape[0], 20_000), replace=False)
        imp = permutation_importance(model, xo[pick], yo[pick], scoring="roc_auc", n_repeats=3, random_state=0)
        top = np.argsort(-imp.importances_mean)[:15]
        importance[spec.name] = [{"feature": cols[i], "auc_drop": float(imp.importances_mean[i])} for i in top]

    out: dict = {
        "rows": {s: int(m.sum()) for s, m in mask.items()},
        "bad_rate": {s: float(y[m].mean()) for s, m in mask.items()},
        "models": {},
        "uplift_vs_baseline": {},
        "importance": importance,
    }
    for name, s in scores.items():
        out["models"][name] = {split: metrics.summary(y[mask[split]], s[mask[split]]) for split in EVAL_SPLITS}
        out["models"][name]["train_auc"] = metrics.auc(y[mask["train"]], s[mask["train"]])
        out["models"][name]["score_psi_train_oot"] = metrics.psi(s[mask["train"]], s[mask["oot"]])
        if name != c.baseline:
            out["uplift_vs_baseline"][name] = {
                split: metrics.bootstrap_auc_delta(y[mask[split]], scores[c.baseline][mask[split]], s[mask[split]]) for split in EVAL_SPLITS
            }
    table = df.select(KEYS).with_columns([pl.Series(f"score_{n}", s) for n, s in scores.items()])
    return Result(out, table, fitted)
