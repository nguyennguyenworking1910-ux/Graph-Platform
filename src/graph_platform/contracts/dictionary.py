"""Data dictionary: describes every table/column of a source and where it lands in the ontology."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from graph_platform.naming import Snake

SemanticType = Literal[
    "id", "foreign_key", "name", "phone", "email", "national_id", "address", "ip",
    "device_id", "date", "timestamp", "amount", "currency", "category", "status",
    "flag", "numeric", "geo", "text",
]  # fmt: skip


class ColumnDict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    dtype: str
    semantic_type: SemanticType
    description: str
    pii: bool = False
    references: str | None = None  # "table.column" for foreign keys
    maps_to: str | None = None  # "Party.full_name", "TRANSACTS.amount", "Party.key"


class TableDict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    primary_key: list[str] = []
    columns: list[ColumnDict]

    def column(self, name: str) -> ColumnDict | None:
        return next((c for c in self.columns if c.name == name), None)


class DataDictionary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Snake
    domain: Snake
    version: int = 1
    tables: list[TableDict]

    def table(self, name: str) -> TableDict | None:
        return next((t for t in self.tables if t.name == name), None)
