"""Model access for agents: Claude on Amazon Bedrock, with a daily token budget and a full audit trail.

Agents receive profiles (statistics and masked samples), never source rows.
"""

from __future__ import annotations

import json
import re
from typing import Protocol

import anthropic
from anthropic import AnthropicBedrockMantle
from pydantic import BaseModel, ValidationError

from graph_platform.contracts import AgentsConfig
from graph_platform.control.audit import Audit


class BudgetExceeded(RuntimeError):
    pass


class Refused(RuntimeError):
    pass


class LLM(Protocol):
    def complete(self, agent: str, system: str, user: str) -> str: ...


class BedrockLLM:
    def __init__(self, cfg: AgentsConfig, audit: Audit) -> None:
        self.cfg, self.audit = cfg, audit
        self.client = AnthropicBedrockMantle(aws_region=cfg.aws_region)

    def complete(self, agent: str, system: str, user: str) -> str:
        used = self.audit.tokens_today()
        if used >= self.cfg.daily_token_limit:
            self.audit.write(agent, "budget_exceeded", used=used, limit=self.cfg.daily_token_limit)
            raise BudgetExceeded(f"{used} tokens used today, limit {self.cfg.daily_token_limit}")
        try:
            with self.client.messages.stream(
                model=self.cfg.model,
                max_tokens=32000,
                thinking={"type": "adaptive"},
                output_config={"effort": self.cfg.effort},
                system=system,
                messages=[{"role": "user", "content": user}],
            ) as stream:
                msg = stream.get_final_message()
        except anthropic.APIError as exc:
            self.audit.write(agent, "llm_error", model=self.cfg.model, error=type(exc).__name__)
            raise
        self.audit.write(
            agent, "llm_call", model=self.cfg.model, request_id=msg._request_id, stop_reason=msg.stop_reason,
            input_tokens=msg.usage.input_tokens, output_tokens=msg.usage.output_tokens,
        )  # fmt: skip
        if msg.stop_reason == "refusal":
            raise Refused(f"{agent}: model declined the request")
        return "".join(b.text for b in msg.content if b.type == "text")


def parse_json[M: BaseModel](text: str, model: type[M]) -> M:
    """The agent's answer is one JSON object, optionally fenced; it must validate against the contract."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("no JSON object in the answer")
    return model.model_validate(json.loads(match.group(0)))


def ask[M: BaseModel](llm: LLM, agent: str, system: str, user: str, model: type[M], rounds: int = 2) -> M:
    """Ask for a contract object; an answer that fails validation is sent back with the errors."""
    prompt = user
    for _ in range(rounds + 1):
        answer = llm.complete(agent, system, prompt)
        try:
            return parse_json(answer, model)
        except (ValueError, ValidationError) as exc:
            error = str(exc)[:3000]
            prompt = f"{user}\n\nYour previous answer was rejected:\n{error}\n\nReturn the corrected JSON object only."
    raise ValueError(f"{agent}: no valid {model.__name__} after {rounds + 1} attempts: {error}")
