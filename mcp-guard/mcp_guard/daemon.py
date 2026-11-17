"""Background daemon: broker on an asyncio thread, popups on the main thread."""
from __future__ import annotations

import asyncio
import logging
import signal
import threading

from .audit import AuditLog
from .broker import Broker
from .config import Settings, home_dir, load_or_create_token
from .prompters import ScriptedPrompter

log = logging.getLogger("mcp_guard.daemon")


def run_daemon(settings: Settings, headless: str | None = None) -> int:
    if headless:
        prompter = ScriptedPrompter(lambda _r: (headless == "allow", 0.0))
    else:
        try:
            import tkinter  # noqa: F401
        except ImportError:
            log.error("tkinter is not installed (Linux: sudo apt install python3-tk)")
            return 1
        from .ui_tk import TkPrompter
        prompter = TkPrompter(settings.decision_timeout)

    broker = Broker(settings, prompter, AuditLog(home_dir() / "audit.log", settings.log_arguments),
                    load_or_create_token())
    ready, errors, state = threading.Event(), [], {}

    def serve() -> None:
        async def main() -> None:
            try:
                await broker.start()
            except OSError as e:
                errors.append(e)
                ready.set()
                return
            state["loop"], state["stop"] = asyncio.get_running_loop(), asyncio.Event()
            ready.set()
            await state["stop"].wait()
            await broker.stop()
        asyncio.run(main())

    thread = threading.Thread(target=serve, name="broker", daemon=True)
    thread.start()
    ready.wait(10)
    if errors:
        log.error("cannot start (is another MCP Guard daemon already running on port %s?): %s",
                  settings.port, errors[0])
        return 1

    def shutdown(*_a) -> None:
        state["loop"].call_soon_threadsafe(state["stop"].set)
        prompter.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, shutdown)
        except (ValueError, OSError):
            pass
    log.info("MCP Guard daemon running (%s). Ctrl+C to quit.", "headless:" + headless if headless else "GUI")
    try:
        prompter.run()                     # blocks until stop(); Tk needs the main thread
    except KeyboardInterrupt:
        shutdown()
    thread.join(5)
    return 0
