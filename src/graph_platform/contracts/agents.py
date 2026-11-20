"""Agent runtime config of a tenant."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AgentsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    aws_region: str
    model: str  # Bedrock model id, e.g. anthropic.claude-opus-5-5
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    daily_token_limit: int = Field(gt=0)  # input + output tokens per UTC day; beyond it no model call is made
    max_repair_rounds: int = Field(3, ge=0)
