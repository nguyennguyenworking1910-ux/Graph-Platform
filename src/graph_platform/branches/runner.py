"""Run a branch contract: plugin -> metrics, scores, report, MLflow run and registered models."""

from __future__ import annotations

import importlib
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import torch

from graph_platform import features
from graph_platform.config import Settings
from graph_platform.contracts import BranchContract, load_yaml
from graph_platform.features.snapshot import Snapshot, open_snapshot
from graph_platform.graph import graph_dir
from graph_platform.storage import Principal, resolve


@dataclass
class Context:
    settings: Settings
    tenant: str
    contract: BranchContract

    def snapshot(self, as_of: date) -> Snapshot:
        """Point-in-time graph under the experiment's scope (the tenant standard unless the experiment overrides it)."""
        scope = self.contract.graph_scope or features.load_scope(self.settings, self.tenant)
        return open_snapshot(
            graph_dir(self.settings, self.tenant, principal="branches"), as_of, self.settings.ontology_of(self.tenant), scope
        )


@dataclass
class Result:
    metrics: dict
    table: pl.DataFrame  # scores or clusters
    models: dict[str, object] = field(default_factory=dict)


def load_contract(settings: Settings, tenant: str, name: str) -> BranchContract:
    return load_yaml(BranchContract, settings.tenant(tenant).branches / f"{name}.yaml")


def branches_dir(settings: Settings, tenant: str) -> Path:
    return resolve(settings, Principal(tenant, "platform", "branches"), "branches", tenant=tenant, mode="w")


def _flat(d: dict, prefix: str = "") -> dict[str, float]:
    out: dict[str, float] = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out |= _flat(v, f"{prefix}{k}.")
        elif isinstance(v, int | float) and not isinstance(v, bool):
            out[f"{prefix}{k}"] = float(v)
    return out


def _report(c: BranchContract, tenant: str, m: dict, run_id: str) -> str:
    lines = [
        f"# Branch report: {c.name} ({tenant})",
        "",
        f"Run `{run_id}` | owner `{c.owner_domain}` | family `{c.family}` | plugin `{c.plugin}`",
        "",
    ]
    if c.models:
        lab, sp = c.label, c.split
        lines += [
            f"Label: `{lab.attribute}` >= {lab.bad_threshold} within {lab.performance_window_months} months"
            + (f", indeterminate from {lab.indeterminate_from}" if lab.indeterminate_from is not None else "")
            + ".",
            f"Split: train {sp.train.start}..{sp.train.end} (test = {sp.test_fraction:.0%} of parties), oot {sp.oot.start}..{sp.oot.end}.",
            "",
            "| Split | Rows | Bad rate |",
            "|---|---|---|",
            *[f"| {s} | {m['rows'][s]} | {m['bad_rate'][s]:.4f} |" for s in m["rows"]],
            "",
            "| Model | Test AUC | Test KS | OOT AUC | OOT Gini | OOT KS | Train AUC | Score PSI train->oot |",
            "|---|---|---|---|---|---|---|---|",
        ]
        lines += [
            f"| {n} | {v['test']['auc']:.4f} | {v['test']['ks']:.4f} | {v['oot']['auc']:.4f} | {v['oot']['gini']:.4f} | "
            f"{v['oot']['ks']:.4f} | {v['train_auc']:.4f} | {v['score_psi_train_oot']:.4f} |"
            for n, v in m["models"].items()
        ]
        lines += ["", f"| Model vs `{c.baseline}` | Split | AUC uplift | 95% CI |", "|---|---|---|---|"]
        lines += [
            f"| {n} | {s} | {u['delta']:+.4f} | [{u['ci_low']:+.4f}, {u['ci_high']:+.4f}] |"
            for n, by in m["uplift_vs_baseline"].items()
            for s, u in by.items()
        ]
        for n, rows in m["importance"].items():
            lines += ["", f"| Top features of {n} (permutation, oot) | AUC drop |", "|---|---|"]
            lines += [f"| `{i['feature']}` | {i['auc_drop']:.4f} |" for i in rows]
    else:
        lines += ["| Metric | Value |", "|---|---|"] + [f"| {k} | {v:.4f} |" for k, v in _flat(m).items()]
    lines += ["", "Synthetic data: these numbers prove the pipeline works end to end, not business value."]
    return "\n".join(lines) + "\n"


def run_branch(settings: Settings, tenant: str, name: str, *, evaluate: bool = False) -> tuple[dict, Path]:
    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
    import mlflow

    c = load_contract(settings, tenant, name)
    plugin = importlib.import_module(f"graph_platform.branches.plugins.{c.plugin}")
    result: Result = plugin.run(Context(settings, tenant, c))
    if evaluate:
        from graph_platform.branches.evaluate import EVALUATORS

        result.metrics["ground_truth"] = EVALUATORS[c.plugin](settings, tenant, result.table)

    base = branches_dir(settings, tenant)
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    out = base / name / run_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "metrics.json").write_text(json.dumps(result.metrics, indent=2), encoding="utf-8")
    (out / "report.md").write_text(_report(c, tenant, result.metrics, run_id), encoding="utf-8")
    result.table.write_parquet(out / "output.parquet")

    mlflow.set_tracking_uri(f"sqlite:///{base / 'mlflow.db'}")
    mlflow.set_registry_uri(f"sqlite:///{base / 'mlflow.db'}")
    experiment = f"{tenant}.{name}"
    if mlflow.get_experiment_by_name(experiment) is None:
        mlflow.create_experiment(experiment, artifact_location=(base / "mlflow_artifacts" / name).as_uri())
    mlflow.set_experiment(experiment)
    with mlflow.start_run(run_name=run_id):
        mlflow.log_params({"plugin": c.plugin, "family": c.family, "owner_domain": c.owner_domain, "run_id": run_id})
        mlflow.log_dict(c.model_dump(mode="json"), "experiment.json")
        mlflow.log_metrics(_flat(result.metrics))
        mlflow.log_artifact(str(out / "report.md"))
        for model_name, model in result.models.items():
            registered = f"{tenant}.{name}.{model_name}"
            if isinstance(model, torch.nn.Module):
                # graph models take a sparse adjacency, which the traced MLflow flavor cannot express: keep the weights
                torch.save(model.state_dict(), out / f"{model_name}.pt")
                mlflow.log_artifact(str(out / f"{model_name}.pt"))
            else:
                # the model was trained in this process, so its tree predictor type is trusted
                mlflow.sklearn.log_model(
                    model, name=model_name, registered_model_name=registered, pip_requirements=["graph-platform"],
                    skops_trusted_types=["sklearn.ensemble._hist_gradient_boosting.predictor.TreePredictor"],
                )  # fmt: skip
    return result.metrics, out
