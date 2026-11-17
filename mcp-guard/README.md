# MCP Guard

A Python app that sits in front of the MCP servers used by **Claude Desktop** and **Claude Web**,
holds every tool call, and shows an always-on-top **Allow / Block** popup. Allowed calls are
released to the real server; blocked calls are never executed and Claude gets a clear denial it
can relay to you.

```
                         ┌──────────────────────────────────────────────┐
                         │  mcp-guard daemon (background, one per user)  │
  Claude Desktop         │  ┌────────┐  decide()   ┌──────────────────┐ │
  ──stdio──► stdio proxy ┼─►│ Broker │────────────►│ Tk popup (topmost)│ │
       │     (per server)│  │ 127.0.0.1 + token    │ one per request  │ │
       │                 │  └────────┘ ◄───────────└──────────────────┘ │
  Claude Web             │      │ rules · timeout · audit.log            │
  ──HTTPS──► tunnel ──►  │      │                                        │
       HTTP reverse proxy┼──────┘                                        │
       │                 └──────────────────────────────────────────────┘
       ▼ only if allowed
   real MCP server
```

## How interception works (and why it differs per client)

| Client | How MCP traffic flows | Where MCP Guard sits |
|---|---|---|
| **Claude Desktop** – local servers in `claude_desktop_config.json` | Desktop spawns each server as a child process and speaks JSON-RPC over its stdin/stdout | `mcp-guard install` rewrites each entry so Desktop launches **`mcp-guard proxy-stdio -- <original command>`**. The proxy spawns the real server and relays messages, **holding `tools/call` until you decide**. |
| **Claude Web** (and Desktop's remote connectors) | Remote MCP servers are called **from Anthropic's cloud**, not from your machine, so nothing local can see the traffic | You run **`mcp-guard proxy-http --upstream <real URL>`**, expose it with a tunnel, and register the **tunnel URL** as the custom connector. The proxy is a Streamable-HTTP reverse proxy that gates `tools/call` POSTs and streams everything else (SSE included) through. |

This is a deliberate design choice: the only places a user-space app *can* reliably and honestly
intercept MCP are the stdio pipe (Desktop) and the server endpoint (Web). I avoided packet sniffing,
TLS MITM, or patching Claude's binaries — they're fragile, break on updates, and are a security
liability. It also means that approvals are enforced **before** execution, not merely observed.

Per request: `Claude ─► proxy ─► (TCP to daemon) ─► popup ─► decision ─► proxy forwards or answers locally`.

### What the popup shows
Client (taken from the MCP `initialize` → `clientInfo`), transport, MCP server name, method, tool
name, and the full (truncated at 6 000 chars) pretty-printed arguments, plus a live countdown.
Buttons are disabled for 0.7 s after a popup appears and default focus is on **Block**, so a keystroke
meant for another window can't approve a call. `Esc` or closing the window = Block.
Optional "don't ask again for `server:tool`" is remembered **until the daemon restarts**.

### What Claude sees on Block
For `tools/call`, a normal tool result with `isError: true` and the text
*"MCP Guard: this request was not executed. blocked by user"* — the MCP-recommended way to report a
failed tool, so Claude explains it instead of crashing. Other guarded methods get JSON-RPC error `-32001`.

## Install

Requirements: Python ≥ 3.10 with Tkinter (macOS/Windows python.org builds include it;
Debian/Ubuntu: `sudo apt install python3-tk`).

```bash
git clone <this repo> && cd mcp-guard
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e .
```

## Run

### 1. Start the daemon (keep it running)
```bash
mcp-guard daemon
```
Preview the popup any time: `mcp-guard simulate --tool delete_file --args '{"path":"/tmp/x"}'`.

### 2a. Claude Desktop
```bash
mcp-guard install --dry-run      # see which servers would be wrapped
mcp-guard install                # wraps every stdio server; backs up to *.mcpguard.bak
```
Fully quit and reopen Claude Desktop. Undo with `mcp-guard uninstall`.
Config location is auto-detected (`~/Library/Application Support/Claude/…`, `%APPDATA%\Claude\…`,
`~/.config/Claude/…`); override with `--config`. The wrapped entry keeps its `env`, and `install`
is idempotent. **Run `install` from the same virtualenv** — its Python path is written into the config.

### 2b. Claude Web (custom connector)
```bash
# the real remote server you want to supervise
mcp-guard proxy-http --name notes --upstream https://mcp.example.com/mcp \
    --listen-port 8765 --secret 4f9c1e7a2b   # --secret = unguessable first path segment
cloudflared tunnel --url http://127.0.0.1:8765      # or ngrok http 8765
```
In Claude: *Settings → Connectors → Add custom connector* →
`https://<tunnel-host>/4f9c1e7a2b/mcp`. (Custom connector availability depends on your plan.)
If the upstream uses OAuth, also pass `--public-url https://<tunnel-host>` so discovery documents
are rewritten to point at the proxy (best-effort; see limitations).

### 3. Autostart (so it "runs in the background")
* **macOS** – a LaunchAgent in `~/Library/LaunchAgents` with `ProgramArguments = [<venv>/bin/python, -m, mcp_guard, daemon]`, `RunAtLoad`, `KeepAlive`.
* **Windows** – Task Scheduler → "At log on" → `<venv>\Scripts\pythonw.exe -m mcp_guard daemon`.
* **Linux** – `systemd --user` unit with `ExecStart=<venv>/bin/python -m mcp_guard daemon` and `Restart=on-failure`.

## Configuration
`~/.mcp-guard/config.json` (see `examples/config.example.json`); `MCP_GUARD_HOME` relocates the folder.

| Key | Default | Meaning |
|---|---|---|
| `decision_timeout` | `60` | Seconds before an unanswered popup auto-**blocks** |
| `fail_mode` | `"block"` | What a proxy does if the daemon is unreachable: `block` or `allow` |
| `allow_rules` / `block_rules` | `[]` | `fnmatch` patterns on `server:tool`. **Block beats allow**; rules skip the popup |
| `guarded_methods` | `["tools/call"]` | Add `"resources/read"`, `"prompts/get"` to gate those too |
| `log_arguments` | `false` | Audit log stores only a SHA-256 of arguments unless `true` |
| `port` | `47615` | Daemon's loopback port (env `MCP_GUARD_PORT`) |

Audit trail: `~/.mcp-guard/audit.log` (JSON lines: who/what/decision/why).

## Reliability & edge cases

| Situation | Behaviour |
|---|---|
| Many simultaneous requests | Each is its own task + TCP connection + popup (cascaded). Pending calls never block unrelated traffic (`tools/list`, notifications, other calls). Tested. |
| User never answers | Auto-block after `decision_timeout`; popup closes itself |
| Claude cancels while popup is open (`notifications/cancelled`, or the HTTP client hangs up) | Popup is withdrawn, server never sees the call, no response is sent (per MCP spec) |
| Daemon not running / crashed / wrong token | Proxy **fails closed** (configurable `fail_mode`), with a denial that explains why |
| Popup/UI throws | Broker converts to Block; error is logged |
| Claude Desktop quits / proxy gets SIGTERM | stdin closed → child server shut down gracefully (then terminate → kill); open popups withdrawn |
| Real server crashes | Proxy exits with its code so Claude Desktop shows the server as failed |
| Second daemon started | Refuses (port in use) with a clear message |
| Malformed JSON / `tools/call` with no id | Non-JSON is passed to the server to report the error; id-less guarded calls are dropped (fail closed) |

## Security model
* IPC is bound to **127.0.0.1 only** (the daemon refuses anything else) and every request needs a 256-bit
  token stored in `~/.mcp-guard/token` (mode 0600), compared in constant time → other local users/sites can't forge approvals.
* **Fail closed** everywhere: timeout, crash, bad input, unreachable daemon ⇒ Block.
* Approval happens **before** the request reaches the server; MCP Guard never edits arguments.
* Arguments aren't written to the audit log by default (they can contain secrets).
* `proxy-http` exposes your upstream to whoever knows the tunnel URL, so use `--secret` and a private tunnel.
  Anyone who can reach the proxy can also trigger popups on your screen (a nuisance, not a bypass).

## Limitations (honest list)
* Gates `tools/call` by default. Other traffic (listing tools, notifications) is relayed unmodified.
* **Desktop Extensions (.mcpb/.dxt)** and servers not listed in `claude_desktop_config.json` aren't wrapped.
* Servers Desktop connects to as *remote connectors* run in Anthropic's cloud — use `proxy-http` for those.
* `proxy-http` + OAuth upstreams: discovery rewriting is implemented but I could only unit-test the no-auth path.
* Claude's own HTTP timeouts apply to remote tool calls; keep `decision_timeout` modest for Web.
* JSON-RPC batches (removed from the spec in 2025-06-18) are split on stdio; on HTTP a batch is blocked if any call in it is blocked.
* Tk popups on macOS may need you to click once to focus the first time; they still appear topmost.

## Code map
```
mcp_guard/
  cli.py          commands: daemon, install, uninstall, proxy-stdio, proxy-http, simulate
  stdio_proxy.py  Claude Desktop interception (async relay, per-request tasks)
  http_proxy.py   Claude Web interception (aiohttp reverse proxy, SSE streaming)
  broker.py       decision service: auth, rules, timeout, cancellation, concurrency
  client.py       proxy→daemon call with fail-safe
  ui_tk.py        popup UI (Tk main thread ⇄ asyncio via thread-safe queue)
  prompters.py    Prompter interface + ScriptedPrompter (headless/tests)
  protocol.py     JSON-RPC/MCP parsing + denial responses (pure functions)
  installer.py    safe, reversible Claude Desktop config rewrite
  audit.py · config.py · models.py · daemon.py
examples/demo_server.py   dependency-free MCP server (echo, get_time, delete_file) for demos/tests
tests/                    19 tests: protocol, broker, installer, stdio e2e, http e2e
```

## Tests
```bash
pip install -e ".[dev]" && pytest -q
```
The e2e tests launch the *real* proxy in front of `demo_server.py` and prove, via the server's own
execution log, that blocked calls never reach it; they also cover concurrency, cancellation,
daemon-down fail-closed/fail-open, and the HTTP proxy.

## Demo script (for the video)
1. `mcp-guard daemon` in a terminal; add `examples/demo_server.py` to Claude Desktop config; `mcp-guard install`; restart Desktop.
2. Ask Claude: *"Use the demo server to echo 'hello'"* → popup → **Allow** → Claude shows the result.
3. Ask: *"Delete /tmp/report.docx with the demo tool"* → popup → **Block** → Claude says the user denied it; `~/.mcp-guard/audit.log` shows both decisions.
4. Ask for two tools at once → two stacked popups, decide in any order.
5. (Web) run `proxy-http` + tunnel, add as connector in claude.ai, repeat an allow and a block.
