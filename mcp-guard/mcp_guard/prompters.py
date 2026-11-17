"""Prompter interface + a scriptable implementation for headless use and tests."""
from __future__ import annotations

import asyncio
import threading
from typing import Callable, Protocol

from .models import ApprovalRequest, Decision


class Prompter(Protocol):
    async def ask(self, req: ApprovalRequest) -> Decision:
        """Resolve with the user's decision. Must clean up its UI if cancelled."""

    def run(self) -> None:
        """Block on the *main thread* (GUI toolkits require it) until ``stop()``."""

    def stop(self) -> None: ...


class ScriptedPrompter:
    """Decides via ``policy(req) -> (allow, delay_seconds)``. No GUI."""

    def __init__(self, policy: Callable[[ApprovalRequest], tuple] | None = None):
        self.policy = policy or (lambda req: (True, 0.0))
        self.seen: list[ApprovalRequest] = []
        self.cancelled: list[str] = []
        self._stop = threading.Event()

    async def ask(self, req: ApprovalRequest) -> Decision:
        self.seen.append(req)
        allow, delay = self.policy(req)
        try:
            if delay:
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            self.cancelled.append(req.id)
            raise
        return Decision(allow, "scripted decision", "user")

    def run(self) -> None:
        self._stop.wait()

    def stop(self) -> None:
        self._stop.set()
