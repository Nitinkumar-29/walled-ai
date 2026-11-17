#!/usr/bin/env python3
"""Tiny dependency-free MCP stdio server for demos and tests.

Tools: echo, get_time, delete_file (harmless: it only *records* the call). If
MCP_DEMO_LOG is set, every executed tool call is appended there, which lets tests prove
that a blocked call never reached the server.
"""
import datetime
import json
import os
import sys

LOG = os.environ.get("MCP_DEMO_LOG")
TOOLS = [
    {"name": "echo", "description": "Echo text back.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "get_time", "description": "Current UTC time.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "delete_file", "description": "Pretend to delete a file (demo only; nothing is deleted).",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
]


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def run_tool(name, args):
    if LOG:
        with open(LOG, "a") as f:
            f.write(f"EXEC {name} {json.dumps(args)}\n")
    if name == "echo":
        return str(args.get("text", ""))
    if name == "get_time":
        return datetime.datetime.now(datetime.timezone.utc).isoformat()
    if name == "delete_file":
        return f"(demo) pretended to delete {args.get('path')}"
    raise KeyError(name)


for line in sys.stdin:
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    mid, method = msg.get("id"), msg.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
            "serverInfo": {"name": "mcp-guard-demo", "version": "0.1"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        p = msg.get("params", {})
        try:
            text = run_tool(p.get("name"), p.get("arguments", {}))
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}]}})
        except KeyError:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "unknown tool"}})
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}})
