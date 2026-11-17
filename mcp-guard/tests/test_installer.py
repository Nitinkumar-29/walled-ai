import json

from mcp_guard import installer


def test_wrap_unwrap_roundtrip(tmp_path):
    cfg = tmp_path / "claude_desktop_config.json"
    original = {"mcpServers": {
        "fs": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
               "env": {"A": "1"}},
        "remote": {"url": "https://example.com/mcp"}}, "other": True}
    cfg.write_text(json.dumps(original))

    assert installer.install(cfg) == ["fs"]
    wrapped = json.loads(cfg.read_text())
    fs = wrapped["mcpServers"]["fs"]
    assert fs["args"][:3] == ["-m", "mcp_guard", "proxy-stdio"] and fs["env"] == {"A": "1"}
    assert wrapped["mcpServers"]["remote"] == original["mcpServers"]["remote"]
    assert installer.install(cfg) == []                       # idempotent
    assert (tmp_path / "claude_desktop_config.json.mcpguard.bak").exists()

    assert installer.uninstall(cfg) == ["fs"]
    assert json.loads(cfg.read_text()) == original


def test_dry_run_does_not_write(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"mcpServers": {"x": {"command": "python"}}}))
    before = cfg.read_text()
    assert installer.install(cfg, dry_run=True) == ["x"]
    assert cfg.read_text() == before
