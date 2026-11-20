"""Declarative contracts. Agents author these; deterministic engines execute them."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel

from graph_platform.contracts.agents import AgentsConfig
from graph_platform.contracts.branch import BranchContract
from graph_platform.contracts.change_proposal import ChangeProposal, GateResult
from graph_platform.contracts.dictionary import ColumnDict, DataDictionary, TableDict
from graph_platform.contracts.graph_scope import GraphScope
from graph_platform.contracts.mapping_spec import (
    EdgeMap,
    EndpointRef,
    Expr,
    GateThresholds,
    MappingSpec,
    NodeMap,
    TableMap,
    Tf,
)
from graph_platform.contracts.onboarding import DomainContract, SourceDecl

SCHEMAS: dict[str, type[BaseModel]] = {
    "domain": DomainContract,
    "dictionary": DataDictionary,
    "mapping_spec": MappingSpec,
    "branch": BranchContract,
    "graph_scope": GraphScope,
    "change_proposal": ChangeProposal,
}


def load_yaml[M: BaseModel](model: type[M], path: Path) -> M:
    return model.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


def dump_yaml(obj: BaseModel, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = obj.model_dump(mode="json", exclude_defaults=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


__all__ = [
    "SCHEMAS", "AgentsConfig", "BranchContract", "ChangeProposal", "ColumnDict", "DataDictionary",
    "DomainContract", "EdgeMap", "EndpointRef", "Expr", "GateResult", "GateThresholds", "GraphScope",
    "MappingSpec", "NodeMap", "SourceDecl", "TableDict", "TableMap", "Tf",
    "dump_yaml", "load_yaml",
]  # fmt: skip
