import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_GUARD_HOME", str(tmp_path / "home"))
    return tmp_path / "home"
