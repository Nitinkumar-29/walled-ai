"""Append-only JSONL audit trail of every decision."""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path

from .models import ApprovalRequest, Decision

log = logging.getLogger("mcp_guard.audit")


class AuditLog:
    def __init__(self, path: Path | None, log_arguments: bool = False):
        self.path, self.log_arguments, self._lock = path, log_arguments, threading.Lock()

    def write(self, req: ApprovalRequest, d: Decision) -> None:
        args = json.dumps(req.arguments, sort_keys=True, default=str)
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "id": req.id, "client": req.client,
                 "transport": req.transport, "server": req.server, "method": req.method,
                 "target": req.target, "allow": d.allow, "decided_by": d.decided_by, "reason": d.reason}
        if self.log_arguments:
            entry["arguments"] = req.arguments
        else:
            entry["args_sha256"] = hashlib.sha256(args.encode()).hexdigest()
        log.info("%s %s/%s -> %s (%s)", req.id, req.server, req.target,
                 "ALLOW" if d.allow else "BLOCK", d.decided_by)
        if self.path is None:
            return
        try:
            with self._lock, open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as e:  # auditing must never break request handling
            log.warning("cannot write audit log: %s", e)
