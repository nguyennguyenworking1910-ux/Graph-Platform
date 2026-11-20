"""Append-only audit log of everything agents and the supervisor do."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from graph_platform.config import Settings
from graph_platform.storage import Principal, resolve


def control_dir(settings: Settings, tenant: str) -> Path:
    path = resolve(settings, Principal(tenant, "platform", "control"), "control", tenant=tenant, mode="w")
    path.mkdir(parents=True, exist_ok=True)
    return path


class Audit:
    def __init__(self, settings: Settings, tenant: str) -> None:
        self.tenant = tenant
        self.path = control_dir(settings, tenant) / "audit.jsonl"

    def write(self, actor: str, action: str, **detail: object) -> None:
        row = {"ts": datetime.now(UTC).isoformat(timespec="seconds"), "tenant": self.tenant, "actor": actor, "action": action, **detail}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")

    def rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line]

    def tokens_today(self) -> int:
        today = datetime.now(UTC).date().isoformat()
        return sum(
            r.get("input_tokens", 0) + r.get("output_tokens", 0) for r in self.rows() if r["action"] == "llm_call" and r["ts"][:10] == today
        )
