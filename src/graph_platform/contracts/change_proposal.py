"""Change proposal: how an agent asks to change a declarative artifact."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

Kind = Literal["dictionary", "mapping_spec", "branch", "er_rules"]
Action = Literal["create", "update", "delete"]
Status = Literal["proposed", "gated", "applied", "rejected", "needs_human"]


class GateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gate: str
    passed: bool
    detail: dict[str, Any] = {}


class ChangeProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    tenant: str
    kind: Kind
    action: Action
    target: str  # conf path relative to the tenant conf dir
    payload: dict[str, Any] | None
    author: str
    rationale: str
    destructive: bool = False
    status: Status = "proposed"
    gates: list[GateResult] = []
    created_at: datetime
