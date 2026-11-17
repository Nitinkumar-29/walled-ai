"""Man-in-the-middle for a *local* (stdio) MCP server, used by Claude Desktop.

Claude Desktop launches ``mcp-guard proxy-stdio -- <real server command>`` instead of
the real server. We spawn the real server as a child and relay newline-delimited
JSON-RPC both ways. ``tools/call`` requests from the client are *held* until the daemon
returns a decision; everything else flows through untouched, so one pending popup never
stalls other traffic. NOTE: stdout belongs to the protocol; all logging goes to stderr.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import shutil
import signal
import sys
import threading
from typing import Any

from .client import request_decision
from .config import STREAM_LIMIT, Settings
from .models import ApprovalRequest, Decision
from .protocol import GuardedCall, client_label, denial_response, parse_guarded

log = logging.getLogger("mcp_guard.stdio")


def _encode(msg: Any) -> bytes:
    return json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"


class StdioProxy:
    def __init__(self, name: str, command: list[str], settings: Settings):
        self.name, self.command, self.settings = name, command, settings
        self.client_name = "Claude Desktop"
        self._child: asyncio.subprocess.Process | None = None
        self._pending: dict[Any, asyncio.Task] = {}     # in-flight guarded calls by JSON-RPC id
        self._anon = itertools.count()
        self._out_lock = threading.Lock()
        self._client_gone = False

    # ---- plumbing ---------------------------------------------------------
    async def _to_server(self, data: bytes) -> None:
        if self._child and self._child.stdin and not self._child.stdin.is_closing():
            self._child.stdin.write(data if data.endswith(b"\n") else data + b"\n")
            await self._child.stdin.drain()

    def _to_client(self, data: bytes) -> None:
        if self._client_gone:
            return
        try:
            with self._out_lock:
                sys.stdout.buffer.write(data if data.endswith(b"\n") else data + b"\n")
                sys.stdout.buffer.flush()
        except (BrokenPipeError, OSError):
            self._client_gone = True

    @staticmethod
    def _read_stdin(loop: asyncio.AbstractEventLoop, inbox: asyncio.Queue) -> None:
        # A thread (not connect_read_pipe) because asyncio can't read stdin pipes on Windows.
        try:
            for line in iter(sys.stdin.buffer.readline, b""):
                loop.call_soon_threadsafe(inbox.put_nowait, line)
        except (RuntimeError, ValueError):
            return
        try:
            loop.call_soon_threadsafe(inbox.put_nowait, None)
        except RuntimeError:
            pass

    # ---- client -> server -------------------------------------------------
    async def _pump_client(self, inbox: asyncio.Queue) -> None:
        while (line := await inbox.get()) is not None:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                await self._to_server(line)     # let the real server produce the parse error
                continue
            if isinstance(msg, list):           # JSON-RPC batch (removed in MCP 2025-06-18): split it
                for item in msg:
                    await self._on_message(item, None)
            else:
                await self._on_message(msg, line)

    async def _on_message(self, msg: Any, raw: bytes | None) -> None:
        method = msg.get("method") if isinstance(msg, dict) else None
        params = msg.get("params") if isinstance(msg, dict) else None
        if method == "initialize":
            self.client_name = client_label(msg) or self.client_name
        elif method == "notifications/cancelled" and isinstance(params, dict):
            task = self._pending.get(json.dumps(params.get("requestId")))
            if task is not None:          # still waiting for the user: withdraw, server never saw it
                task.cancel()
                return
        call = parse_guarded(msg, self.settings.guarded_methods)
        if call is None:
            await self._to_server(raw or _encode(msg))
            return
        key = json.dumps(call.rpc_id) if call.has_id else f"anon-{next(self._anon)}"
        task = asyncio.create_task(self._guard(msg, raw, call))
        self._pending[key] = task
        task.add_done_callback(lambda t, k=key: self._pending.pop(k, None) if self._pending.get(k) is t else None)

    async def _guard(self, msg: dict, raw: bytes | None, call: GuardedCall) -> None:
        req = ApprovalRequest(server=self.name, method=call.method, target=call.target,
                              arguments=call.arguments, client=self.client_name, transport="stdio")
        try:
            decision = await request_decision(self.settings, req)
        except asyncio.CancelledError:
            log.info("%s %s cancelled by client while pending", self.name, call.target)
            return                         # per MCP: no response for a cancelled request
        except Exception as e:             # fail closed on anything unexpected
            log.exception("approval failed")
            decision = Decision(False, f"internal error: {e}", "error")
        if decision.allow:
            await self._to_server(raw or _encode(msg))
        else:
            resp = denial_response(call, decision.reason)
            if resp is not None:
                self._to_client(_encode(resp))

    # ---- server -> client -------------------------------------------------
    async def _pump_server(self) -> None:
        assert self._child and self._child.stdout
        while True:
            try:
                line = await self._child.stdout.readline()
            except ValueError:
                log.error("server emitted a line over the size limit; stopping")
                return
            if not line:
                return
            self._to_client(line)

    # ---- lifecycle ----------------------------------------------------------
    async def run(self) -> int:
        exe = shutil.which(self.command[0]) or self.command[0]   # resolves npx -> npx.cmd on Windows
        try:
            self._child = await asyncio.create_subprocess_exec(
                exe, *self.command[1:], stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, limit=STREAM_LIMIT)  # stderr inherited -> Claude's logs
        except OSError as e:
            log.error("cannot start MCP server %r: %s", self.command, e)
            return 127
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError, ValueError):
                pass                                              # Windows
        inbox: asyncio.Queue = asyncio.Queue()
        threading.Thread(target=self._read_stdin, args=(loop, inbox), daemon=True).start()
        client = asyncio.create_task(self._pump_client(inbox))
        server = asyncio.create_task(self._pump_server())
        stopper = asyncio.create_task(stop.wait())
        await asyncio.wait({client, server, stopper}, return_when=asyncio.FIRST_COMPLETED)

        for t in list(self._pending.values()):
            t.cancel()                    # closes sockets -> daemon withdraws any open popups
        await self._shutdown_child(graceful=server.done() is False)
        for t in (client, server, stopper):
            t.cancel()
        await asyncio.gather(client, server, stopper, return_exceptions=True)
        return self._child.returncode or 0

    async def _shutdown_child(self, graceful: bool) -> None:
        child = self._child
        if child.returncode is None and child.stdin:
            child.stdin.close()           # standard MCP stdio shutdown: close stdin first
        try:
            await asyncio.wait_for(child.wait(), 5 if graceful else 0.1)
        except asyncio.TimeoutError:
            child.terminate()
            try:
                await asyncio.wait_for(child.wait(), 3)
            except asyncio.TimeoutError:
                child.kill()
                await child.wait()


def run_stdio_proxy(name: str, command: list[str]) -> int:
    return asyncio.run(StdioProxy(name, command, Settings.load()).run())
