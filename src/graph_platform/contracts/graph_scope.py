"""Graph scope: compute treatment that bounds time range and fan-out before any feature or model runs."""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, ConfigDict, Field


class GraphScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lookback_days: int = Field(365, gt=0)  # event-like edges older than this are out of scope
    windowed_edges: list[str] = ["TRANSACTS", "PURCHASES"]  # edge types the lookback applies to
    max_degree: int = Field(50, gt=0)  # peers kept per party, strongest first
    min_weight: int = Field(1, gt=0)  # interactions needed for a peer edge
    device_fanout: int = Field(20, gt=1)  # devices shared by more parties than this are ignored
    model_hops: int = Field(2, ge=1)  # GNNs run on this many hops around the population, not the whole graph

    @property
    def scope_id(self) -> str:
        """Features computed under different scopes never share a directory."""
        return "scope_" + hashlib.sha1(self.model_dump_json().encode()).hexdigest()[:10]
