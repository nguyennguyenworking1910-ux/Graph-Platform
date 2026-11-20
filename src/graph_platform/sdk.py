"""Python entry point for ML/AI teams.

from graph_platform import sdk
df = sdk.training_table("alpha_group", "credit_score")
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from graph_platform import branches, features
from graph_platform.config import Settings
from graph_platform.contracts import BranchContract


def experiment(tenant: str, name: str) -> BranchContract:
    """The experiment config as a typed object; edit it in memory to try variants."""
    return branches.load_contract(Settings.load(), tenant, name)


def training_table(tenant: str, experiment_name: str, config: BranchContract | None = None) -> pl.DataFrame:
    """Point-in-time features + label + split for a node-level experiment."""
    s = Settings.load()
    return branches.training_table(s, tenant, config or branches.load_contract(s, tenant, experiment_name))


def feature_table(tenant: str, as_of: date, families: list[str]) -> pl.DataFrame:
    """One row per party with the requested feature families at as_of (tenant standard scope), built if absent."""
    s = Settings.load()
    scope = features.load_scope(s, tenant)
    if features.missing(s, tenant, [as_of], families, scope):
        features.build(s, tenant, [as_of], families, scope)
    return features.load(s, tenant, as_of, families, scope)


def party_table(tenant: str, name: str, as_of: date) -> pl.DataFrame:
    """A table a business keeps whole next to the graph, as known at as_of: the latest row per party, keyed by `party_id`."""
    import duckdb

    from graph_platform.storage import Principal, canonical_files, resolve

    s = Settings.load()
    who = Principal(tenant, "platform", "branches")
    files = canonical_files(s, who, tenant, "tables", name)
    if not files:
        raise KeyError(f"no table '{name}' is attached in tenant '{tenant}'")
    resolved = resolve(s, who, "shared", tenant=tenant) / "party_resolved.parquet"
    query = """SELECT r.global_party_id AS party_id, t.* EXCLUDE (node_id)
               FROM read_parquet(?, union_by_name=true) t JOIN read_parquet(?) r USING (node_id)
               WHERE t.event_time < CAST(? AS TIMESTAMP)
               QUALIFY row_number() OVER (PARTITION BY r.global_party_id ORDER BY t.event_time DESC) = 1"""
    return duckdb.connect().sql(query, params=[files, str(resolved), as_of]).pl()


def feature_registry(tenant: str) -> pl.DataFrame:
    import json

    s = Settings.load()
    path = features.features_dir(s, tenant, features.load_scope(s, tenant), role="branches") / "registry.json"
    return pl.DataFrame(json.loads(path.read_text())["features"])


def run(tenant: str, experiment_name: str) -> tuple[dict, Path]:
    """Run an experiment end to end; returns (metrics, output directory)."""
    return branches.run_branch(Settings.load(), tenant, experiment_name)


def ask(tenant: str, party_id: str, question: str) -> str:
    """Answer a question about one party from its latest context document (needs the party_knowledge experiment and Bedrock)."""
    from graph_platform.agents.llm import BedrockLLM
    from graph_platform.agents.roles import answer
    from graph_platform.contracts import AgentsConfig, load_yaml
    from graph_platform.control.audit import Audit

    s = Settings.load()
    latest = sorted(p for p in (branches.runner.branches_dir(s, tenant) / "party_knowledge").iterdir() if p.is_dir())[-1]
    docs = pl.read_parquet(latest / "output.parquet").filter(pl.col("party_id") == party_id)
    if docs.is_empty():
        raise KeyError(f"no context document for {party_id}")
    llm = BedrockLLM(load_yaml(AgentsConfig, s.tenant(tenant).agents), Audit(s, tenant))
    return answer(llm, question, docs["context"][0])
