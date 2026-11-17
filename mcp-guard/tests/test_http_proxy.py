import asyncio
import json

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from mcp_guard.audit import AuditLog
from mcp_guard.broker import Broker
from mcp_guard.config import Settings
from mcp_guard.http_proxy import HttpProxy
from mcp_guard.prompters import ScriptedPrompter

TOKEN = "h" * 64


def test_http_allow_block_and_passthrough(home, monkeypatch):
    async def go():
        hits = []

        async def mcp(request):
            body = await request.json()
            hits.append(body["method"])
            hdr = {"Mcp-Session-Id": "sess1"} if body["method"] == "initialize" else {}
            return web.json_response({"jsonrpc": "2.0", "id": body.get("id"), "result": {"ok": True}}, headers=hdr)

        up = web.Application()
        up.router.add_post("/mcp", mcp)
        upstream = TestServer(up)
        await upstream.start_server()

        home.mkdir(parents=True, exist_ok=True)
        (home / "token").write_text(TOKEN)
        prompter = ScriptedPrompter(lambda r: (r.target == "good", 0))
        broker = Broker(Settings(port=0), prompter, AuditLog(None), TOKEN)
        await broker.start()
        monkeypatch.setenv("MCP_GUARD_PORT", str(broker.port))

        proxy = HttpProxy("remote", f"http://127.0.0.1:{upstream.port}/mcp", Settings.load())
        runner = await proxy.serve("127.0.0.1", 0)
        base = f"http://127.0.0.1:{runner.addresses[0][1]}/mcp"
        try:
            async with aiohttp.ClientSession() as c:
                async def post(payload, **h):
                    async with c.post(base, json=payload, headers=h) as r:
                        return await r.json()

                init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"clientInfo": {"name": "claude-ai", "version": "9"}}}
                assert (await post(init))["result"]["ok"]

                call = lambda i, n: {"jsonrpc": "2.0", "id": i, "method": "tools/call",
                                     "params": {"name": n, "arguments": {"q": 1}}}
                assert (await post(call(2, "good"), **{"Mcp-Session-Id": "sess1"}))["result"]["ok"]
                bad = await post(call(3, "evil"))
                assert bad["result"]["isError"] is True
                assert hits == ["initialize", "tools/call"]          # "evil" never reached upstream
                assert prompter.seen[0].client == "claude-ai 9"       # session -> client mapping
                assert prompter.seen[0].transport == "http"
        finally:
            await runner.cleanup()
            await broker.stop()
            await upstream.close()
    asyncio.run(go())
