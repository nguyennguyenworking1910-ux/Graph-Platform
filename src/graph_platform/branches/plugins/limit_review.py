"""Credit limit review: combine a risk experiment and a value experiment into one recommended action per customer."""

from __future__ import annotations

import polars as pl

from graph_platform.branches.runner import Context, Result, branches_dir

ACTIONS = ("Tăng hạn mức", "Giữ nguyên", "Xem xét giảm")


def _latest(ctx: Context, experiment: str, column: str) -> pl.DataFrame:
    runs = sorted(p for p in (branches_dir(ctx.settings, ctx.tenant) / experiment).iterdir() if p.is_dir())
    return pl.read_parquet(runs[-1] / "output.parquet").select("party_id", column)


def run(ctx: Context) -> Result:
    par = {
        "risk_experiment": "credit_early_warning",
        "value_experiment": "customer_ltv",
        "low_risk_quantile": 0.5,
        "high_risk_quantile": 0.9,
    } | ctx.contract.params
    risk = _latest(ctx, par["risk_experiment"], "score").rename({"score": "risk_score"})
    value = _latest(ctx, par["value_experiment"], "predicted").rename({"predicted": "predicted_value"})
    df = risk.join(value, on="party_id")  # only customers who are current borrowers
    low, high = df["risk_score"].quantile(float(par["low_risk_quantile"])), df["risk_score"].quantile(float(par["high_risk_quantile"]))
    valuable = df["predicted_value"].median()
    table = (
        df.with_columns(
            action=pl.when(pl.col("risk_score") >= high).then(pl.lit(ACTIONS[2]))
            .when((pl.col("risk_score") <= low) & (pl.col("predicted_value") >= valuable)).then(pl.lit(ACTIONS[0]))
            .otherwise(pl.lit(ACTIONS[1]))
        )
        .sort(pl.col("action").replace_strict({a: i for i, a in enumerate(ACTIONS)}), pl.col("predicted_value"), descending=[False, True])
        .with_row_index("rank", 1)
    )  # fmt: skip
    counts = dict(table.group_by("action").len().iter_rows())
    return Result({"customer_cnt": table.height, "increase_cnt": counts.get(ACTIONS[0], 0), "reduce_cnt": counts.get(ACTIONS[2], 0)}, table)
