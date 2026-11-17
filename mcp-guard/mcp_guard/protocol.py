"""Pure helpers for the bits of JSON-RPC / MCP we care about."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

BLOCK_CODE = -32001  # implementation-defined JSON-RPC error: "denied by user"


@dataclass
class GuardedCall:
    rpc_id: Any
    has_id: bool
    method: str
    target: str
    arguments: Any


def parse_guarded(msg: Any, guarded_methods: Iterable[str]) -> Optional[GuardedCall]:
    """Return a GuardedCall if ``msg`` is a request we must gate, else None.

    A guarded method *without* an id (a malformed notification) is still returned so
    the caller can fail closed rather than silently forwarding it.
    """
    if not isinstance(msg, dict) or msg.get("method") not in set(guarded_methods):
        return None
    params = msg.get("params")
    if not isinstance(params, dict):
        params = {}
    target = params.get("name") or params.get("uri") or "<unknown>"
    return GuardedCall(msg.get("id"), "id" in msg, msg["method"], str(target), params.get("arguments", {}))


def denial_response(call: GuardedCall, reason: str) -> Optional[dict]:
    """JSON-RPC reply sent to the client instead of running the call.

    For tools/call we return a *tool result* with isError=true (the MCP-recommended way to
    report tool failure) so Claude can read the refusal and explain it to the user.
    Other methods get a protocol-level error.
    """
    if not call.has_id:
        return None
    text = f"MCP Guard: this request was not executed. {reason}".strip()
    if call.method == "tools/call":
        return {"jsonrpc": "2.0", "id": call.rpc_id,
                "result": {"content": [{"type": "text", "text": text}], "isError": True}}
    return {"jsonrpc": "2.0", "id": call.rpc_id, "error": {"code": BLOCK_CODE, "message": text}}


def client_label(initialize_msg: dict) -> Optional[str]:
    """Human-readable client name from an ``initialize`` request's clientInfo."""
    info = (initialize_msg.get("params") or {}).get("clientInfo")
    if isinstance(info, dict) and info.get("name"):
        v = info.get("version")
        return f"{info['name']} {v}" if v else str(info["name"])
    return None
