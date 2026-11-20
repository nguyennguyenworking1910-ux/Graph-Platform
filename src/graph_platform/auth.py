"""Accounts for the web interface: who may sign in, to which tenants and business units, with which role."""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

from graph_platform.naming import Snake

Role = Literal["viewer", "analyst", "admin"]
# what each role may open; a higher role includes the lower ones
PAGES = {
    "viewer": ["Giới thiệu", "Câu hỏi nghiệp vụ", "Tra cứu khách hàng"],
    "analyst": ["Giới thiệu", "Câu hỏi nghiệp vụ", "Tra cứu khách hàng", "Dữ liệu", "Thiết lập bài toán"],
    "admin": ["Giới thiệu", "Câu hỏi nghiệp vụ", "Tra cứu khách hàng", "Dữ liệu", "Thiết lập bài toán", "Phê duyệt"],
}


class User(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: Snake
    name: str
    role: Role
    tenants: list[Snake]
    domains: list[Snake] = []  # business units the user may change; empty = every unit of their tenants
    password: str  # scrypt$<salt hex>$<hash hex>

    def may_change(self, domain: str) -> bool:
        return self.role != "viewer" and (not self.domains or domain in self.domains)


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    return f"scrypt${salt.hex()}${hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1).hex()}"


def verify(password: str, stored: str) -> bool:
    _, salt, digest = stored.split("$")
    candidate = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1).hex()
    return hmac.compare_digest(candidate, digest)


def load_users(path: Path) -> dict[str, User]:
    if not path.exists():
        return {}
    return {u["username"]: User.model_validate(u) for u in yaml.safe_load(path.read_text(encoding="utf-8")) or []}


def save_user(path: Path, user: User) -> None:
    users = load_users(path) | {user.username: user}
    path.write_text(yaml.safe_dump([u.model_dump() for u in users.values()], sort_keys=False, allow_unicode=True), encoding="utf-8")
    path.chmod(0o600)


def sign_in(path: Path, username: str, password: str) -> User | None:
    user = load_users(path).get(username)
    return user if user and verify(password, user.password) else None
