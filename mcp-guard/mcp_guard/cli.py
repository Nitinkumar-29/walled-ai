"""Command-line entry point: ``mcp-guard <command>``."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from . import __version__
from .config import Settings


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mcp-guard", description=__doc__)
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("daemon", help="run the background approval service (shows the popups)")
    d.add_argument("--headless", choices=["allow", "block"], help="no GUI, auto-decide (testing only)")

    for name, h in (("install", "wrap Claude Desktop's stdio MCP servers"),
                    ("uninstall", "restore the original Claude Desktop config")):
        s = sub.add_parser(name, help=h)
        s.add_argument("--config", type=Path, help="path to claude_desktop_config.json")
        s.add_argument("--dry-run", action="store_true")

    s = sub.add_parser("proxy-stdio", help="(internal) stdio proxy launched by Claude Desktop")
    s.add_argument("--name", required=True, help="server name shown in popups")
    s.add_argument("command", nargs=argparse.REMAINDER, help="-- <real server command> [args...]")

    h = sub.add_parser("proxy-http", help="reverse proxy for a remote MCP server (Claude Web)")
    h.add_argument("--name", required=True)
    h.add_argument("--upstream", required=True, help="real MCP endpoint, e.g. https://example.com/mcp")
    h.add_argument("--listen-host", default="127.0.0.1")
    h.add_argument("--listen-port", type=int, default=8765)
    h.add_argument("--public-url", help="tunnel URL Claude will use (rewrites OAuth discovery URLs)")
    h.add_argument("--secret", help="require this first path segment, e.g. https://tunnel/<secret>/mcp")

    m = sub.add_parser("simulate", help="send a fake request to the daemon to preview the popup")
    m.add_argument("--server", default="demo")
    m.add_argument("--tool", default="delete_file")
    m.add_argument("--args", default='{"path": "/tmp/report.docx"}')
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(stream=sys.stderr, level=os.environ.get("MCP_GUARD_LOG", "INFO").upper(),
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    a = _parser().parse_args(argv)

    if a.cmd == "daemon":
        from .daemon import run_daemon
        return run_daemon(Settings.load(), a.headless)

    if a.cmd in ("install", "uninstall"):
        from . import installer
        path = a.config or installer.default_config_path()
        if not path.exists():
            print(f"Config not found: {path}\nUse --config to point at claude_desktop_config.json.")
            return 1
        names = (installer.install if a.cmd == "install" else installer.uninstall)(path, a.dry_run)
        verb = ("would wrap" if a.dry_run else "wrapped") if a.cmd == "install" else \
               ("would restore" if a.dry_run else "restored")
        print(f"{verb}: {', '.join(names) or 'nothing (already up to date)'}\nConfig: {path}")
        if names and not a.dry_run:
            print("Restart Claude Desktop (fully quit it) for the change to take effect.")
        return 0

    if a.cmd == "proxy-stdio":
        from .stdio_proxy import run_stdio_proxy
        command = a.command[1:] if a.command[:1] == ["--"] else a.command
        if not command:
            print("usage: mcp-guard proxy-stdio --name NAME -- COMMAND [ARGS...]", file=sys.stderr)
            return 2
        return run_stdio_proxy(a.name, command)

    if a.cmd == "proxy-http":
        from .http_proxy import run_http_proxy
        return run_http_proxy(a.name, a.upstream, a.listen_host, a.listen_port, a.public_url, a.secret)

    if a.cmd == "simulate":
        from .client import request_decision
        from .models import ApprovalRequest
        req = ApprovalRequest(server=a.server, method="tools/call", target=a.tool,
                              arguments=json.loads(a.args), client="simulate", transport="cli")
        d = asyncio.run(request_decision(Settings.load(), req))
        print(("ALLOWED" if d.allow else "BLOCKED"), "-", d.reason, f"[{d.decided_by}]")
        return 0 if d.allow else 3
    return 2
