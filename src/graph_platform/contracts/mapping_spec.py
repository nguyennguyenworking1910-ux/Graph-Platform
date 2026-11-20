"""Mapping spec: declarative source-table -> ontology mapping. Transforms are whitelisted."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from graph_platform.naming import Snake

TransformName = Literal[
    "trim", "lower", "upper", "cast", "parse_date", "parse_timestamp", "epoch_s", "epoch_ms",
    "normalize_phone", "normalize_email", "digits_only", "hash", "map_values", "scale",
]  # fmt: skip


class Tf(BaseModel):
    """`name`, `name:arg`, or `{name: arg}`."""

    model_config = ConfigDict(extra="forbid")

    name: TransformName
    arg: Any = None

    @model_validator(mode="before")
    @classmethod
    def _parse(cls, v: Any) -> Any:
        if isinstance(v, str):
            name, _, arg = v.partition(":")
            return {"name": name, "arg": arg or None}
        if isinstance(v, dict) and len(v) == 1 and "name" not in v:
            ((name, arg),) = v.items()
            return {"name": name, "arg": arg}
        return v


class Expr(BaseModel):
    """A column (list = coalesce), a constant, or a `-`-joined concat, piped through transforms."""

    model_config = ConfigDict(extra="forbid")

    col: str | list[str] | None = None
    const: str | int | float | bool | None = None
    concat: list[str] | None = None
    tf: list[Tf] = []

    @model_validator(mode="after")
    def _one_of(self) -> Expr:
        if sum(v is not None for v in (self.col, self.const, self.concat)) != 1:
            raise ValueError("exactly one of col/const/concat is required")
        return self

    def columns(self) -> list[str]:
        if self.concat is not None:
            return list(self.concat)
        if self.col is None:
            return []
        return [self.col] if isinstance(self.col, str) else list(self.col)


class When(BaseModel):
    """Only rows whose column holds one of these values: one source table can carry several kinds of record."""

    model_config = ConfigDict(extra="forbid")

    col: str
    is_in: list[str | int | float | bool]


class NodeMap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    subtype: str
    key: Expr
    when: When | None = None
    unique: bool = False  # one source row per node: duplicates are gated
    time: Expr | None = None
    attrs: dict[str, Expr] = {}


class EndpointRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    subtype: str | None = None  # required for global-scope nodes
    key: Expr


class EdgeMap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    src: EndpointRef
    dst: EndpointRef
    when: When | None = None
    key: Expr | None = None  # dedupe key; without it identical rows collapse
    time: Expr
    attrs: dict[str, Expr] = {}


class Attached(BaseModel):
    """Keep a whole table next to the graph: one row per party and time, every non-personal column as the source has it.

    For data that describes a party but is not a relationship: wide scoring tables, balances by day, survey answers.
    """

    model_config = ConfigDict(extra="forbid")

    name: Snake
    party: Expr  # the same key the source's Party node uses
    time: Expr  # when the row became known


class TableMap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table: str
    nodes: list[NodeMap] = []
    edges: list[EdgeMap] = []
    attached: Attached | None = None

    def columns_used(self) -> set[str]:
        """Source columns the mapping reads. With `attached`, the whole table is kept."""
        exprs: list[Expr | None] = []
        for n in self.nodes:
            exprs += [n.key, n.time, *n.attrs.values()]
        for e in self.edges:
            exprs += [e.src.key, e.dst.key, e.key, e.time, *e.attrs.values()]
        chosen = {m.when.col for m in [*self.nodes, *self.edges] if m.when}
        return {c for x in exprs if x for c in x.columns()} | chosen


class GateThresholds(BaseModel):
    """Owned by the platform per tenant (gates.yaml); a mapping spec cannot change them."""

    model_config = ConfigDict(extra="forbid")

    max_key_null_rate: float = 0.01
    min_time_parse_rate: float = 0.99
    min_endpoint_coverage: float = 0.99
    max_duplicate_rate: float = 0.05


class MappingSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Snake
    domain: Snake
    version: int = 1
    tables: list[TableMap]
