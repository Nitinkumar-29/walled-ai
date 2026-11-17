from mcp_guard.protocol import client_label, denial_response, parse_guarded

G = ["tools/call"]


def test_parse_tool_call():
    c = parse_guarded({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                       "params": {"name": "echo", "arguments": {"text": "hi"}}}, G)
    assert (c.rpc_id, c.target, c.arguments) == (7, "echo", {"text": "hi"})


def test_ignores_other_methods_and_garbage():
    assert parse_guarded({"id": 1, "method": "tools/list"}, G) is None
    assert parse_guarded([1, 2], G) is None
    assert parse_guarded("x", G) is None


def test_malformed_params_still_guarded():
    c = parse_guarded({"id": 1, "method": "tools/call", "params": "oops"}, G)
    assert c is not None and c.target == "<unknown>"


def test_denial_shapes():
    c = parse_guarded({"id": "a", "method": "tools/call", "params": {"name": "x"}}, G)
    r = denial_response(c, "nope")
    assert r["id"] == "a" and r["result"]["isError"] is True
    c2 = parse_guarded({"id": 2, "method": "resources/read", "params": {"uri": "file:///x"}}, ["resources/read"])
    assert denial_response(c2, "nope")["error"]["code"] == -32001
    c3 = parse_guarded({"method": "tools/call", "params": {"name": "x"}}, G)   # no id
    assert c3 is not None and denial_response(c3, "nope") is None


def test_client_label():
    assert client_label({"params": {"clientInfo": {"name": "claude-ai", "version": "0.1.0"}}}) == "claude-ai 0.1.0"
    assert client_label({"params": {}}) is None
