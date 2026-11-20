#!/usr/bin/env python3
"""Small stateless Streamable HTTP MCP server for Claude Web demos."""
from __future__ import annotations

import argparse
import datetime
import json
import os
from pathlib import Path
from typing import Any

from aiohttp import web

TOOLS = [
    {"name": "echo", "description": "Echo text back.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                     "required": ["text"]}},
    {"name": "get_time", "description": "Current UTC time.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "delete_file", "description": "Pretend to delete a file (demo only; nothing is deleted).",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}},
                     "required": ["path"]}},
]


def _record_execution(log_path: Path | None, name: str, arguments: dict[str, Any]) -> None:
    if log_path is not None:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"EXEC {name} {json.dumps(arguments, ensure_ascii=False)}\n")


def _call_tool(name: str, arguments: dict[str, Any]) -> str:
    if name == "echo":
        return str(arguments.get("text", ""))
    if name == "get_time":
        return datetime.datetime.now(datetime.timezone.utc).isoformat()
    if name == "delete_file":
        return f"(demo) pretended to delete {arguments.get('path')}"
    raise KeyError(name)


def _response(rpc_id: Any, result: dict[str, Any]) -> web.Response:
    return web.json_response({"jsonrpc": "2.0", "id": rpc_id, "result": result})


def _error(rpc_id: Any, code: int, message: str, status: int = 200) -> web.Response:
    return web.json_response({"jsonrpc": "2.0", "id": rpc_id,
                              "error": {"code": code, "message": message}}, status=status)


def create_app(log_path: str | Path | None = None) -> web.Application:
    configured_log = log_path if log_path is not None else os.environ.get("MCP_DEMO_LOG")
    execution_log = Path(configured_log) if configured_log else None

    async def handle(request: web.Request) -> web.Response:
        if request.method != "POST":
            return web.Response(status=405, headers={"Allow": "POST"})
        try:
            message = json.loads(await request.text())
        except json.JSONDecodeError:
            return _error(None, -32700, "Parse error", status=400)
        if (not isinstance(message, dict) or message.get("jsonrpc") != "2.0"
                or not isinstance(message.get("method"), str)):
            return _error(None, -32600, "Invalid Request", status=400)

        method = message["method"]
        if "id" not in message:
            if method.startswith("notifications/"):
                return web.Response(status=202)
            return _error(None, -32600, "Invalid Request", status=400)
        rpc_id = message["id"]
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}

        if method == "initialize":
            return _response(rpc_id, {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mcp-guard-http-demo", "version": "0.1"},
            })
        if method == "ping":
            return _response(rpc_id, {})
        if method == "tools/list":
            return _response(rpc_id, {"tools": TOOLS})
        if method == "tools/call":
            name, arguments = params.get("name"), params.get("arguments", {})
            if not isinstance(name, str) or not isinstance(arguments, dict):
                return _error(rpc_id, -32602, "Invalid tools/call parameters")
            try:
                text = _call_tool(name, arguments)
            except KeyError:
                return _error(rpc_id, -32602, "Unknown tool")
            _record_execution(execution_log, name, arguments)
            return _response(rpc_id, {"content": [{"type": "text", "text": text}]})
        return _error(rpc_id, -32601, "Method not found")

    app = web.Application()
    app.router.add_route("*", "/mcp", handle)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    web.run_app(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
