"""Reverse proxy for *remote* (Streamable HTTP) MCP servers, used by Claude Web.

Claude Web / Claude's custom connectors call remote MCP servers from Anthropic's cloud,
so the only point under the user's control is the server's URL. Register this proxy's
(tunnelled) URL as the connector instead; every ``tools/call`` POST is held for approval
and then either relayed (SSE streams are passed through chunk by chunk) or answered locally
with a denial. Everything else (GET streams, DELETE, OAuth discovery) is relayed untouched.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import signal
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

from .client import request_decision
from .config import Settings
from .models import ApprovalRequest, Decision
from .protocol import client_label, denial_response, parse_guarded

log = logging.getLogger("mcp_guard.http")

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
       "trailers", "transfer-encoding", "upgrade"}


class HttpProxy:
    def __init__(self, name: str, upstream: str, settings: Settings,
                 public_url: str | None = None, secret: str | None = None):
        u = urlsplit(upstream)
        if u.scheme not in ("http", "https") or not u.netloc:
            raise ValueError(f"invalid upstream URL: {upstream!r}")
        self.name, self.settings, self.secret = name, settings, secret
        self.origin = f"{u.scheme}://{u.netloc}"
        p = urlsplit(public_url) if public_url else None
        self.public_origin = f"{p.scheme}://{p.netloc}" if p else None
        self.sessions: dict[str, str] = {}            # Mcp-Session-Id -> client label
        self.session: aiohttp.ClientSession | None = None

    def make_app(self) -> web.Application:
        app = web.Application(client_max_size=64 * 1024 * 1024)
        app.router.add_route("*", "/{tail:.*}", self.handle)
        app.on_startup.append(self._startup)
        app.on_cleanup.append(self._cleanup)
        return app

    async def _startup(self, _app) -> None:
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=10),
                                             auto_decompress=True)

    async def _cleanup(self, _app) -> None:
        if self.session:
            await self.session.close()

    def _strip_secret(self, raw_path: str) -> str | None:
        if not self.secret:
            return raw_path
        first, _, rest = raw_path.lstrip("/").partition("/")
        if not hmac.compare_digest(first.encode(), self.secret.encode()):
            return None
        return "/" + rest

    async def handle(self, request: web.Request) -> web.StreamResponse:
        path_qs = self._strip_secret(request.raw_path)
        if path_qs is None:
            return web.Response(status=404)
        body = await request.read() if request.can_read_body else b""
        guarded, parsed, init_client = [], None, None
        if request.method == "POST" and body:
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = None
            for m in (parsed if isinstance(parsed, list) else [parsed]):
                if isinstance(m, dict) and m.get("method") == "initialize":
                    init_client = client_label(m)
                call = parse_guarded(m, self.settings.guarded_methods)
                if call:
                    guarded.append(call)
        if guarded:
            client = self.sessions.get(request.headers.get("Mcp-Session-Id", ""), "Claude (remote connector)")
            decisions = await asyncio.gather(*(self._ask(c, client) for c in guarded))
            if not all(d.allow for d in decisions):
                reasons = [denial_response(c, d.reason if not d.allow else
                                           "Another request in the same batch was blocked.")
                           for c, d in zip(guarded, decisions)]
                reasons = [r for r in reasons if r is not None]
                if isinstance(parsed, list):
                    return web.json_response(reasons) if reasons else web.Response(status=202)
                return web.json_response(reasons[0]) if reasons else web.Response(status=202)
        return await self._forward(request, path_qs, body, init_client)

    async def _ask(self, call, client: str) -> Decision:
        req = ApprovalRequest(server=self.name, method=call.method, target=call.target,
                              arguments=call.arguments, client=client, transport="http")
        try:
            return await request_decision(self.settings, req)
        except asyncio.CancelledError:
            raise                                    # browser/cloud hung up: popup gets withdrawn
        except Exception as e:
            log.exception("approval failed")
            return Decision(False, f"internal error: {e}", "error")

    def _rewrite(self, text: str) -> str:
        return text.replace(self.origin, self.public_origin) if self.public_origin else text

    async def _forward(self, request, path_qs: str, body: bytes, init_client: str | None):
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in HOP | {"host", "content-length"}}
        try:
            async with self.session.request(request.method, self.origin + path_qs, data=body or None,
                                            headers=headers, allow_redirects=False) as up:
                sid = up.headers.get("Mcp-Session-Id")
                if init_client and sid:
                    if len(self.sessions) > 1000:
                        self.sessions.clear()
                    self.sessions[sid] = init_client
                hdrs = [(k, self._rewrite(v) if k.lower() in ("www-authenticate", "location") else v)
                        for k, v in up.headers.items()
                        if k.lower() not in HOP | {"content-length", "content-encoding"}]
                if self.public_origin and path_qs.startswith("/.well-known/") and up.status == 200:
                    data = (await up.read()).replace(self.origin.encode(), self.public_origin.encode())
                    resp = web.Response(status=up.status, body=data)
                    for k, v in hdrs:
                        resp.headers.add(k, v)
                    return resp
                resp = web.StreamResponse(status=up.status)
                for k, v in hdrs:
                    resp.headers.add(k, v)
                await resp.prepare(request)
                async for chunk in up.content.iter_any():    # keeps SSE streaming live
                    await resp.write(chunk)
                await resp.write_eof()
                return resp
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning("upstream error: %r", e)
            return web.json_response({"jsonrpc": "2.0", "id": None,
                                      "error": {"code": -32603, "message": f"upstream unreachable: {e}"}},
                                     status=502)

    async def serve(self, host: str, port: int) -> web.AppRunner:
        runner = web.AppRunner(self.make_app(), handler_cancellation=True)
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
        return runner


def run_http_proxy(name: str, upstream: str, host: str, port: int,
                   public_url: str | None, secret: str | None) -> int:
    proxy = HttpProxy(name, upstream, Settings.load(), public_url, secret)

    async def main() -> None:
        runner = await proxy.serve(host, port)
        log.info("proxying http://%s:%s -> %s  (expose it with a tunnel, then add that URL "
                 "as a custom connector in Claude)", host, port, upstream)
        stop = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                asyncio.get_running_loop().add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        try:
            await stop.wait()
        finally:
            await runner.cleanup()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    return 0
