"""Ontology model and validation.

The ontology is declared in YAML (conf/ontology/core.yaml). Everything
downstream (mapping specs, canonical tables, graph projection, features)
is checked against it, so a malformed ontology must fail loudly here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from graph_platform.naming import Pascal, Snake, UpperSnake

AttrType = Literal["string", "int", "double", "bool", "date", "timestamp"]

# DuckDB type used when materializing an ontology attribute.
SQL_TYPES: dict[str, str] = {
    "string": "VARCHAR",
    "int": "BIGINT",
    "double": "DOUBLE",
    "bool": "BOOLEAN",
    "date": "DATE",
    "timestamp": "TIMESTAMP",
}


class OntologyError(ValueError):
    """Raised when the ontology file is structurally invalid."""


class AttributeDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Snake
    type: AttrType
    pii: bool = False
    extension: bool = False  # declared by a tenant, not by the core ontology


class NodeType(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Pascal
    scope: Literal["source", "global"]
    subtypes: list[Snake]
    time_column: Literal["valid_from", "event_time"]
    attributes: list[AttributeDef] = []

    def attribute(self, name: str) -> AttributeDef | None:
        return next((a for a in self.attributes if a.name == name), None)


class EdgeType(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: UpperSnake
    source: list[str]
    target: list[str]
    time_column: Literal["event_time"]
    attributes: list[AttributeDef] = []

    def attribute(self, name: str) -> AttributeDef | None:
        return next((a for a in self.attributes if a.name == name), None)


class Ontology(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    lineage_columns: list[str]
    nodes: list[NodeType]
    edges: list[EdgeType]

    @model_validator(mode="after")
    def _check_consistency(self) -> Ontology:
        problems: list[str] = []

        node_names = [n.name for n in self.nodes]
        edge_names = [e.name for e in self.edges]
        for kind, names in (("node", node_names), ("edge", edge_names)):
            dupes = sorted({n for n in names if names.count(n) > 1})
            if dupes:
                problems.append(f"duplicate {kind} types: {dupes}")

        for required in ("source_system", "domain", "source_pk", "ingested_at"):
            if required not in self.lineage_columns:
                problems.append(f"lineage_columns must include '{required}'")

        reserved = set(self.lineage_columns) | {
            "node_id", "node_type", "subtype", "valid_from", "event_time",
            "edge_type", "src_id", "dst_id", "src_type", "dst_type",
        }  # fmt: skip
        for item in [*self.nodes, *self.edges]:
            attr_names = [a.name for a in item.attributes]
            dupes = sorted({a for a in attr_names if attr_names.count(a) > 1})
            if dupes:
                problems.append(f"{item.name}: duplicate attributes {dupes}")
            clash = sorted(set(attr_names) & reserved)
            if clash:
                problems.append(f"{item.name}: attributes use reserved names {clash}")

        for node in self.nodes:
            if not node.subtypes:
                problems.append(f"{node.name}: at least one subtype is required")

        known = set(node_names)
        for edge in self.edges:
            for side, endpoints in (("source", edge.source), ("target", edge.target)):
                if not endpoints:
                    problems.append(f"{edge.name}: {side} endpoint list is empty")
                unknown = sorted(set(endpoints) - known)
                if unknown:
                    problems.append(f"{edge.name}: unknown {side} node types {unknown}")

        if problems:
            raise ValueError("; ".join(problems))
        return self

    def node(self, name: str) -> NodeType:
        for n in self.nodes:
            if n.name == name:
                return n
        raise KeyError(f"unknown node type: {name}")

    def edge(self, name: str) -> EdgeType:
        for e in self.edges:
            if e.name == name:
                return e
        raise KeyError(f"unknown edge type: {name}")


class Added(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subtypes: list[Snake] = []
    attributes: list[AttributeDef] = []


class Extension(BaseModel):
    """What one tenant adds to the core types for data only its businesses have. It cannot add or change a type."""

    model_config = ConfigDict(extra="forbid")

    nodes: dict[str, Added] = {}
    edges: dict[str, Added] = {}


def extend(core: Ontology, path: Path) -> Ontology:
    """The core ontology plus a tenant's extension file."""
    try:
        ext = Extension.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {})
        raw = core.model_dump()
        for kind, added in (("nodes", ext.nodes), ("edges", ext.edges)):
            known = {item["name"]: item for item in raw[kind]}
            for name, more in added.items():
                if name not in known:
                    raise ValueError(f"{name}: an extension can only add to a core type")
                if kind == "edges" and more.subtypes:
                    raise ValueError(f"{name}: edges have no subtypes")
                known[name]["attributes"] += [a.model_dump() | {"extension": True} for a in more.attributes]
                if kind == "nodes":
                    known[name]["subtypes"] += more.subtypes
        return Ontology.model_validate(raw)
    except (OSError, yaml.YAMLError, ValueError) as exc:
        raise OntologyError(f"invalid ontology extension {path}: {exc}") from exc


def load_ontology(path: Path) -> Ontology:
    """Load and validate an ontology YAML file."""
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise OntologyError(f"cannot read ontology {path}: {exc}") from exc
    try:
        return Ontology.model_validate(raw)
    except ValidationError as exc:
        details = "; ".join(f"{'.'.join(str(p) for p in err['loc']) or 'ontology'}: {err['msg']}" for err in exc.errors())
        raise OntologyError(f"invalid ontology {path}: {details}") from exc
