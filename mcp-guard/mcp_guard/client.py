"""What proxies use to ask the daemon for a decision (fail-safe by design)."""
from __future__ import annotations

import asyncio
import json
import logging

from .config import STREAM_LIMIT, Settings, read_token
from .models import ApprovalRequest, Decision

log = logging.getLogger("mcp_guard.client")


def _failsafe(settings: Settings, why: str) -> Decision:
    allow = settings.fail_mode == "allow"
    log.warning("MCP Guard unavailable (%s); fail_mode=%s -> %s", why, settings.fail_mode,
                "ALLOW" if allow else "BLOCK")
    return Decision(allow, f"MCP Guard could not ask for approval ({why}).", "failsafe")


async def request_decision(settings: Settings, req: ApprovalRequest) -> Decision:
    token = read_token()
    if token is None:
        return _failsafe(settings, "daemon has never been started")
    writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(settings.host, settings.port, limit=STREAM_LIMIT), 3)
        writer.write(json.dumps({"token": token, "request": req.to_wire()}).encode() + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), settings.decision_timeout + 15)
        if not line:
            raise ConnectionError("daemon closed the connection")
        data = json.loads(line)
        if "error" in data:
            raise PermissionError(data["error"])
        return Decision.from_wire(data)
    except asyncio.CancelledError:
        raise  # finally-block closes the socket, which tells the daemon to withdraw the popup
    except (OSError, ValueError, KeyError, asyncio.TimeoutError) as e:
        return _failsafe(settings, f"daemon not reachable: {e!r}" if isinstance(e, OSError) else repr(e))
    finally:
        if writer is not None:
            writer.close()
