"""Data shared between proxies and the daemon."""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class ApprovalRequest:
    """One pending MCP request awaiting a human decision."""
    server: str
    method: str
    target: str                      # tool name (tools/call), URI (resources/read), ...
    arguments: Any = None
    client: str = "unknown"          # e.g. "Claude Desktop", "claude-ai"
    transport: str = "stdio"         # "stdio" | "http"
    id: str = field(default_factory=_new_id)
    created: float = field(default_factory=time.time)

    @property
    def key(self) -> str:
        return f"{self.server}:{self.target}"

    def to_wire(self) -> dict:
        return asdict(self)

    @classmethod
    def from_wire(cls, d: Any) -> "ApprovalRequest":
        if not isinstance(d, dict):
            raise ValueError("request must be a JSON object")
        return cls(
            server=str(d.get("server", "?")),
            method=str(d.get("method", "?")),
            target=str(d.get("target", "?")),
            arguments=d.get("arguments"),
            client=str(d.get("client", "unknown")),
            transport=str(d.get("transport", "stdio")),
            id=str(d.get("id") or _new_id()),
        )


@dataclass
class Decision:
    allow: bool
    reason: str = ""
    decided_by: str = "user"   # user | rule | timeout | failsafe | error
    remember: bool = False

    def to_wire(self) -> dict:
        return asdict(self)

    @classmethod
    def from_wire(cls, d: dict) -> "Decision":
        return cls(bool(d["allow"]), str(d.get("reason", "")), str(d.get("decided_by", "user")),
                   bool(d.get("remember", False)))
