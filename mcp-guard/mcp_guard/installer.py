"""Wrap / unwrap stdio servers in Claude Desktop's claude_desktop_config.json."""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

MARK = ["-m", "mcp_guard", "proxy-stdio"]


def default_config_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    if sys.platform.startswith("win"):
        return Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "Claude" / "claude_desktop_config.json"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "Claude" / "claude_desktop_config.json"


def is_wrapped(entry: dict) -> bool:
    return entry.get("args", [])[:3] == MARK


def wrap_entry(name: str, entry: dict) -> dict:
    """Same entry, but launching our proxy (which launches the original command)."""
    out = dict(entry)                       # keeps env, cwd, etc. — the proxy child inherits them
    out["command"] = sys.executable         # absolute interpreter that has mcp_guard installed
    out["args"] = [*MARK, "--name", name, "--", entry["command"], *entry.get("args", [])]
    return out


def unwrap_entry(entry: dict) -> dict:
    args = entry["args"]
    i = args.index("--")
    out = dict(entry)
    out["command"], out["args"] = args[i + 1], args[i + 2:]
    return out


def _load(path: Path) -> dict:
    return json.loads(path.read_text("utf-8")) if path.exists() else {}


def _save(path: Path, data: dict) -> None:
    bak = path.with_name(path.name + ".mcpguard.bak")
    if path.exists() and not bak.exists():
        shutil.copy2(path, bak)             # one-time backup of the pristine file
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", "utf-8")
    os.replace(tmp, path)                   # atomic: Claude never sees a half-written file


def install(path: Path, dry_run: bool = False) -> list[str]:
    data = _load(path)
    changed = []
    for name, entry in (data.get("mcpServers") or {}).items():
        if isinstance(entry, dict) and entry.get("command") and not is_wrapped(entry):
            data["mcpServers"][name] = wrap_entry(name, entry)
            changed.append(name)
    if changed and not dry_run:
        _save(path, data)
    return changed


def uninstall(path: Path, dry_run: bool = False) -> list[str]:
    data = _load(path)
    changed = []
    for name, entry in (data.get("mcpServers") or {}).items():
        if isinstance(entry, dict) and is_wrapped(entry) and "--" in entry["args"]:
            data["mcpServers"][name] = unwrap_entry(entry)
            changed.append(name)
    if changed and not dry_run:
        _save(path, data)
    return changed
