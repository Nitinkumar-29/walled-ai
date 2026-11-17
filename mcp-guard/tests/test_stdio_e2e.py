"""Spawns the real proxy in front of examples/demo_server.py with an in-process broker."""
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp_guard.audit import AuditLog
from mcp_guard.broker import Broker
from mcp_guard.config import Settings
from mcp_guard.prompters import ScriptedPrompter

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "e" * 64


class Stack:
    def __init__(self, home, policy, port=None, env=None, **settings):
        self.home, self.log = home, home / "server.log"
        self.prompter = ScriptedPrompter(policy)
        self.broker = Broker(Settings(port=0, decision_timeout=5, **settings), self.prompter,
                             AuditLog(home / "audit.log"), TOKEN)
        self.port_override, self.extra_env = port, env or {}
        self.buf = {}

    async def __aenter__(self):
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "token").write_text(TOKEN)
        await self.broker.start()
        port = self.port_override or self.broker.port
        env = {**os.environ, "MCP_GUARD_HOME": str(self.home), "MCP_GUARD_PORT": str(port),
               "MCP_DEMO_LOG": str(self.log), **self.extra_env}
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "mcp_guard", "proxy-stdio", "--name", "demo", "--",
            sys.executable, str(ROOT / "examples/demo_server.py"),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, env=env, cwd=ROOT)
        await self.send({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "claude-ai", "version": "1.0"}}})
        await self.recv(0)
        return self

    async def __aexit__(self, *exc):
        self.proc.stdin.close()
        try:
            await asyncio.wait_for(self.proc.wait(), 8)
        except asyncio.TimeoutError:
            self.proc.kill()
        await self.broker.stop()

    async def send(self, obj):
        self.proc.stdin.write(json.dumps(obj).encode() + b"\n")
        await self.proc.stdin.drain()

    async def call(self, id, tool, **args):
        await self.send({"jsonrpc": "2.0", "id": id, "method": "tools/call",
                         "params": {"name": tool, "arguments": args}})

    async def recv(self, id, timeout=6):
        async def read():
            while id not in self.buf:
                line = await self.proc.stdout.readline()
                assert line, "proxy closed stdout"
                m = json.loads(line)
                self.buf[m.get("id")] = m
            return self.buf.pop(id)
        return await asyncio.wait_for(read(), timeout)

    def executed(self):
        return self.log.read_text() if self.log.exists() else ""


def run(coro):
    return asyncio.run(coro)


def test_allow_and_block(home):
    async def go():
        async with Stack(home, lambda r: (r.target != "delete_file", 0)) as s:
            await s.call(1, "echo", text="hello")
            r = await s.recv(1)
            assert r["result"]["content"][0]["text"] == "hello" and "isError" not in r["result"]

            await s.call(2, "delete_file", path="/precious")
            r = await s.recv(2)
            assert r["result"]["isError"] is True and "not executed" in r["result"]["content"][0]["text"]
            assert "delete_file" not in s.executed() and "echo" in s.executed()
            assert s.prompter.seen[0].client == "claude-ai 1.0"       # clientInfo captured
    run(go())


def test_concurrent_requests_do_not_block_each_other(home):
    async def go():
        async with Stack(home, lambda r: (r.target != "delete_file", 0.8 if r.target == "echo" else 0)) as s:
            await s.call(10, "echo", text="slow-allowed")        # pending ~0.8s
            await s.call(11, "delete_file", path="x")             # blocked instantly
            await s.send({"jsonrpc": "2.0", "id": 12, "method": "tools/list"})   # unguarded passthrough
            assert "tools" in (await s.recv(12, 0.5))["result"]   # not stalled by pending popup
            assert (await s.recv(11, 0.5))["result"]["isError"]
            assert (await s.recv(10))["result"]["content"][0]["text"] == "slow-allowed"
    run(go())


def test_client_cancel_withdraws_pending_request(home):
    async def go():
        async with Stack(home, lambda r: (True, 30)) as s:
            await s.call(5, "echo", text="never")
            await asyncio.sleep(0.5)
            await s.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 5}})
            for _ in range(60):
                if s.prompter.cancelled:
                    break
                await asyncio.sleep(0.05)
            assert s.prompter.cancelled
            await asyncio.sleep(0.2)
            assert s.executed() == ""                              # never reached the real server
    run(go())


def test_daemon_down_fails_closed(home):
    async def go():
        async with Stack(home, lambda r: (True, 0), port=1) as s:  # nothing listens on port 1
            await s.call(1, "echo", text="x")
            r = await s.recv(1)
            assert r["result"]["isError"] and "could not ask" in r["result"]["content"][0]["text"]
            assert s.executed() == ""
    run(go())


def test_daemon_down_fail_open_when_configured(home):
    async def go():
        async with Stack(home, lambda r: (False, 0), port=1, env={"MCP_GUARD_FAIL_MODE": "allow"}) as s:
            await s.call(1, "echo", text="x")
            assert (await s.recv(1))["result"]["content"][0]["text"] == "x"
    run(go())
