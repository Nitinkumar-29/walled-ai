import asyncio
import json
import sys
from pathlib import Path

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from mcp_guard.audit import AuditLog
from mcp_guard.broker import Broker
from mcp_guard.config import Settings
from mcp_guard.http_proxy import HttpProxy
from mcp_guard.prompters import ScriptedPrompter

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "examples"))
from demo_http_server import create_app

TOKEN = "d" * 64


def test_http_demo_allow_and_block_through_proxy(home, monkeypatch, tmp_path):
    async def go():
        log_path = tmp_path / "executed.log"
        upstream = TestServer(create_app(log_path))
        await upstream.start_server()

        home.mkdir(parents=True, exist_ok=True)
        (home / "token").write_text(TOKEN)
        prompter = ScriptedPrompter(lambda req: (req.target == "echo", 0))
        broker = Broker(Settings(port=0), prompter, AuditLog(None), TOKEN)
        await broker.start()
        monkeypatch.setenv("MCP_GUARD_PORT", str(broker.port))

        proxy = HttpProxy("mcp-guard-demo", str(upstream.make_url("/mcp")), Settings.load(),
                          secret="test-secret")
        runner = await proxy.serve("127.0.0.1", 0)
        base = f"http://127.0.0.1:{runner.addresses[0][1]}/test-secret/mcp"
        try:
            async with aiohttp.ClientSession() as client:
                async def post(payload):
                    async with client.post(base, json=payload) as response:
                        return response.status, await response.json()

                status, initialized = await post({
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "claude-ai", "version": "1"}},
                })
                assert status == 200
                assert initialized["result"]["serverInfo"]["name"] == "mcp-guard-http-demo"

                status, listed = await post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
                assert status == 200
                assert [tool["name"] for tool in listed["result"]["tools"]] == [
                    "echo", "get_time", "delete_file"]

                status, allowed = await post({
                    "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "echo", "arguments": {"text": "hello"}},
                })
                assert status == 200
                assert allowed["result"]["content"][0]["text"] == "hello"

                status, blocked = await post({
                    "jsonrpc": "2.0", "id": 4, "method": "tools/call",
                    "params": {"name": "delete_file", "arguments": {"path": "demo.txt"}},
                })
                assert status == 200
                assert blocked["result"]["isError"] is True
                assert "not executed" in blocked["result"]["content"][0]["text"]
                assert log_path.read_text(encoding="utf-8") == 'EXEC echo {"text": "hello"}\n'
                assert [req.target for req in prompter.seen] == ["echo", "delete_file"]
        finally:
            await runner.cleanup()
            await broker.stop()
            await upstream.close()

    asyncio.run(go())
