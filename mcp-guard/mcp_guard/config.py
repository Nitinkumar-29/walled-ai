"""Settings and on-disk locations (``~/.mcp-guard`` unless ``MCP_GUARD_HOME`` is set)."""
from __future__ import annotations

import json
import logging
import os
import secrets
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Optional

log = logging.getLogger("mcp_guard.config")

STREAM_LIMIT = 64 * 1024 * 1024  # max single JSON line we will buffer


def home_dir() -> Path:
    p = Path(os.environ.get("MCP_GUARD_HOME") or Path.home() / ".mcp-guard")
    p.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class Settings:
    host: str = "127.0.0.1"           # daemon IPC address (loopback only)
    port: int = 47615                 # daemon IPC port
    decision_timeout: float = 60.0    # seconds before an unanswered popup auto-blocks
    fail_mode: str = "block"          # proxies' behaviour if daemon unreachable: block | allow
    allow_rules: list = field(default_factory=list)   # fnmatch on "server:tool"
    block_rules: list = field(default_factory=list)   # block beats allow
    guarded_methods: list = field(default_factory=lambda: ["tools/call"])
    log_arguments: bool = False       # audit log stores only a hash of arguments by default

    @classmethod
    def load(cls) -> "Settings":
        s = cls()
        path = home_dir() / "config.json"
        data = {}
        if path.exists():
            try:
                data = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError) as e:
                log.warning("ignoring unreadable %s: %s", path, e)
        known = {f.name for f in fields(cls)}
        for k, v in data.items():
            if k in known:
                setattr(s, k, v)
            else:
                log.warning("unknown config key %r ignored", k)
        # Environment overrides (handy inside Claude Desktop's per-server "env").
        if os.environ.get("MCP_GUARD_PORT"):
            s.port = int(os.environ["MCP_GUARD_PORT"])
        if os.environ.get("MCP_GUARD_TIMEOUT"):
            s.decision_timeout = float(os.environ["MCP_GUARD_TIMEOUT"])
        if os.environ.get("MCP_GUARD_FAIL_MODE"):
            s.fail_mode = os.environ["MCP_GUARD_FAIL_MODE"]
        if s.fail_mode not in ("block", "allow"):
            log.warning("invalid fail_mode %r; using 'block'", s.fail_mode)
            s.fail_mode = "block"
        return s


def read_token() -> Optional[str]:
    path = home_dir() / "token"
    try:
        return path.read_text().strip() or None
    except OSError:
        return None


def load_or_create_token() -> str:
    """Shared secret between daemon and proxies (file is 0600 on POSIX)."""
    tok = read_token()
    if tok:
        return tok
    tok = secrets.token_hex(32)
    path = home_dir() / "token"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok)
    return tok
