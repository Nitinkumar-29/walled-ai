import asyncio
import json

from mcp_guard.audit import AuditLog
from mcp_guard.broker import Broker
from mcp_guard.config import Settings
from mcp_guard.models import ApprovalRequest, Decision
from mcp_guard.prompters import ScriptedPrompter

TOKEN = "t" * 64


def req(tool="echo", server="srv"):
    return ApprovalRequest(server=server, method="tools/call", target=tool, arguments={"a": 1})


def make(policy=None, **kw):
    p = ScriptedPrompter(policy)
    return p, Broker(Settings(port=0, **kw), p, AuditLog(None), TOKEN)


def test_rules_block_beats_allow():
    async def go():
        p, b = make(allow_rules=["srv:*"], block_rules=["srv:delete*"])
        assert (await b.decide(req("echo"))).allow
        d = await b.decide(req("delete_file"))
        assert not d.allow and d.decided_by == "rule"
        assert len(p.seen) == 0          # rules short-circuit the popup
    asyncio.run(go())


def test_timeout_blocks_and_cancels_popup():
    async def go():
        p, b = make(lambda r: (True, 5), decision_timeout=0.1)
        d = await b.decide(req())
        assert not d.allow and d.decided_by == "timeout"
        assert p.cancelled == [p.seen[0].id]
    asyncio.run(go())


def test_prompter_crash_fails_closed():
    async def go():
        p, b = make()
        async def boom(_r): raise RuntimeError("ui died")
        p.ask = boom
        d = await b.decide(req())
        assert not d.allow and d.decided_by == "error"
    asyncio.run(go())


def test_remember_choice():
    async def go():
        p, b = make()
        async def ask(r): return Decision(True, "ok", "user", remember=True)
        p.ask = ask
        await b.decide(req())
        p.ask = None  # a second prompt would crash the test
        d = await b.decide(req())
        assert d.allow and d.decided_by == "rule"
    asyncio.run(go())


async def _roundtrip(port, token, request):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(json.dumps({"token": token, "request": request.to_wire()}).encode() + b"\n")
    await w.drain()
    line = await r.readline()
    w.close()
    return json.loads(line)


def test_tcp_auth_and_concurrency():
    async def go():
        # request "slow" is allowed after 0.3s, "fast" is blocked immediately
        p, b = make(lambda r: (r.target == "slow", 0.3 if r.target == "slow" else 0))
        await b.start()
        try:
            assert (await _roundtrip(b.port, "wrong", req()))["error"] == "unauthorized"
            slow, fast = await asyncio.gather(_roundtrip(b.port, TOKEN, req("slow")),
                                              _roundtrip(b.port, TOKEN, req("fast")))
            assert slow["allow"] is True and fast["allow"] is False
        finally:
            await b.stop()
    asyncio.run(go())


def test_disconnect_withdraws_request():
    async def go():
        p, b = make(lambda r: (True, 30))
        await b.start()
        try:
            r, w = await asyncio.open_connection("127.0.0.1", b.port)
            q = req()
            w.write(json.dumps({"token": TOKEN, "request": q.to_wire()}).encode() + b"\n")
            await w.drain()
            await asyncio.sleep(0.2)
            w.close()
            for _ in range(50):
                if p.cancelled:
                    break
                await asyncio.sleep(0.05)
            assert p.cancelled == [q.id]
        finally:
            await b.stop()
    asyncio.run(go())
