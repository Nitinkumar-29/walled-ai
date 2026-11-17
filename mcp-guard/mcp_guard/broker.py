"""The decision broker: a loopback TCP service that proxies talk to.

Wire format: one JSON line in ``{"token": ..., "request": {...}}``, one JSON line out
(a Decision, or ``{"error": ...}``). One TCP connection == one pending request, so a
proxy withdraws a request (e.g. the client cancelled it) simply by closing the socket.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
from fnmatch import fnmatchcase

from .audit import AuditLog
from .config import STREAM_LIMIT, Settings
from .models import ApprovalRequest, Decision
from .prompters import Prompter

log = logging.getLogger("mcp_guard.broker")


class Broker:
    def __init__(self, settings: Settings, prompter: Prompter, audit: AuditLog, token: str):
        self.settings, self.prompter, self.audit, self.token = settings, prompter, audit, token
        self.port = settings.port
        self._session_rules: dict[str, bool] = {}   # "server:tool" -> allow?
        self._server: asyncio.AbstractServer | None = None
        self._tasks: set[asyncio.Task] = set()

    # ---- policy -----------------------------------------------------------
    def _match_rules(self, req: ApprovalRequest) -> Decision | None:
        key = req.key
        if any(fnmatchcase(key, p) for p in self.settings.block_rules):
            return Decision(False, f"blocked by rule for {key}", "rule")
        if self._session_rules.get(key) is False:
            return Decision(False, f"you chose to always block {key} this session", "rule")
        if any(fnmatchcase(key, p) for p in self.settings.allow_rules):
            return Decision(True, f"allowed by rule for {key}", "rule")
        if self._session_rules.get(key) is True:
            return Decision(True, f"you chose to always allow {key} this session", "rule")
        return None

    async def decide(self, req: ApprovalRequest) -> Decision:
        """Never raises (except CancelledError): any failure becomes a Block."""
        d = self._match_rules(req)
        if d is None:
            try:
                d = await asyncio.wait_for(self.prompter.ask(req), self.settings.decision_timeout)
            except asyncio.TimeoutError:
                d = Decision(False, "no decision within the time limit", "timeout")
            except asyncio.CancelledError:
                self.audit.write(req, Decision(False, "request withdrawn by client", "cancelled"))
                raise
            except Exception as e:  # UI crashed, etc. -> fail closed
                log.exception("prompter failed")
                d = Decision(False, f"internal error: {e}", "error")
            if d.remember and d.decided_by == "user":
                self._session_rules[req.key] = d.allow
        self.audit.write(req, d)
        return d

    # ---- server -----------------------------------------------------------
    async def start(self) -> None:
        if self.settings.host not in ("127.0.0.1", "::1", "localhost"):
            raise OSError("refusing to listen on a non-loopback address")
        self._server = await asyncio.start_server(self._handle, self.settings.host,
                                                  self.settings.port, limit=STREAM_LIMIT)
        self.port = self._server.sockets[0].getsockname()[1]
        log.info("broker listening on %s:%s", self.settings.host, self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._server:
            try:
                await asyncio.wait_for(self._server.wait_closed(), 3)
            except asyncio.TimeoutError:
                pass

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, obj: dict) -> None:
        writer.write(json.dumps(obj).encode() + b"\n")
        await writer.drain()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._tasks.add(asyncio.current_task())
        try:
            try:
                msg = json.loads(await asyncio.wait_for(reader.readline(), 5))
                if not (isinstance(msg, dict)
                        and hmac.compare_digest(str(msg.get("token", "")).encode(), self.token.encode())):
                    await self._reply(writer, {"error": "unauthorized"})
                    return
                req = ApprovalRequest.from_wire(msg.get("request"))
            except (asyncio.TimeoutError, ValueError) as e:
                await self._reply(writer, {"error": f"bad request: {e!r}"})
                return
            decide = asyncio.ensure_future(self.decide(req))
            gone = asyncio.ensure_future(reader.read())   # completes on EOF => proxy hung up
            try:
                await asyncio.wait({decide, gone}, return_when=asyncio.FIRST_COMPLETED)
                if decide.done():
                    await self._reply(writer, decide.result().to_wire())
                else:
                    log.info("requester of %s went away; withdrawing popup", req.id)
            finally:
                for t in (decide, gone):
                    t.cancel()
                await asyncio.gather(decide, gone, return_exceptions=True)
        except Exception:
            log.exception("connection handler failed")
        finally:
            self._tasks.discard(asyncio.current_task())
            writer.close()
