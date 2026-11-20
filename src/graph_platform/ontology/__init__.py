"""Core ontology: the node/edge vocabulary every business domain maps into."""

from graph_platform.ontology.model import (
    AttributeDef,
    EdgeType,
    NodeType,
    Ontology,
    OntologyError,
    extend,
    load_ontology,
)

__all__ = [
    "AttributeDef",
    "EdgeType",
    "NodeType",
    "Ontology",
    "OntologyError",
    "extend",
    "load_ontology",
]
