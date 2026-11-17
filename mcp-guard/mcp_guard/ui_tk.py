"""Tkinter confirmation popups (always-on-top). Tk must own the main thread.

Concurrency model: asyncio code calls ``ask()`` (any thread) which drops a message on a
thread-safe queue; the Tk main loop polls it and opens one popup per request. Resolving a
popup calls back into the asyncio loop with ``call_soon_threadsafe``. Cancelling the
awaiting coroutine (client cancel / timeout / proxy died) posts a "cancel" that closes it.
"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
import time

from .models import ApprovalRequest, Decision

MAX_ARG_CHARS = 6000
ARM_DELAY_MS = 700   # buttons stay disabled briefly so a stray keystroke can't approve a fresh popup


def format_arguments(args) -> str:
    try:
        text = json.dumps(args, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = repr(args)
    if len(text) > MAX_ARG_CHARS:
        text = text[:MAX_ARG_CHARS] + f"\n… [{len(text) - MAX_ARG_CHARS} more characters not shown]"
    return text


class TkPrompter:
    def __init__(self, timeout: float):
        self.timeout = timeout
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._popups: dict[str, "_Popup"] = {}
        self._root = None

    # -- asyncio side ------------------------------------------------------
    async def ask(self, req: ApprovalRequest) -> Decision:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()

        def resolve(d: Decision) -> None:
            loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(d))

        self._q.put(("show", req, resolve))
        try:
            return await fut
        except asyncio.CancelledError:
            self._q.put(("cancel", req.id, None))
            raise

    # -- Tk side (main thread) --------------------------------------------
    def run(self) -> None:
        import tkinter as tk
        self._tk = tk
        self._root = tk.Tk()
        self._root.withdraw()
        self._root.after(100, self._pump)
        self._root.mainloop()

    def stop(self) -> None:
        self._stop.set()

    def _pump(self) -> None:
        try:
            while True:
                kind, payload, resolve = self._q.get_nowait()
                if kind == "show":
                    self._popups[payload.id] = _Popup(self._tk, self._root, payload, resolve, self.timeout,
                                                      len(self._popups), self._popups)
                elif (p := self._popups.pop(payload, None)) is not None:
                    p.close()
        except queue.Empty:
            pass
        if self._stop.is_set():
            for p in list(self._popups.values()):
                p.finish(Decision(False, "MCP Guard is shutting down", "failsafe"))
            self._root.destroy()
            return
        self._root.after(100, self._pump)


class _Popup:
    def __init__(self, tk, root, req: ApprovalRequest, resolve, timeout: float, slot: int, registry: dict):
        from tkinter import ttk
        from tkinter.scrolledtext import ScrolledText

        self.req, self.resolve, self.registry, self.done = req, resolve, registry, False
        self.deadline = time.time() + timeout
        w = self.win = tk.Toplevel(root)
        w.title("MCP Guard — approval required")
        w.geometry(f"580x470+{140 + slot * 28}+{90 + slot * 28}")
        w.attributes("-topmost", True)
        w.protocol("WM_DELETE_WINDOW", lambda: self._choose(False, "popup closed"))
        w.bind("<Escape>", lambda _e: self._choose(False, "blocked by user"))

        f = ttk.Frame(w, padding=12)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="An MCP request is waiting for your decision",
                  font=("TkDefaultFont", 12, "bold")).pack(anchor="w")
        grid = ttk.Frame(f)
        grid.pack(fill="x", pady=8)
        for i, (k, v) in enumerate([("Client", req.client), ("Via", req.transport), ("MCP server", req.server),
                                    ("Method", req.method), ("Tool / target", req.target)]):
            ttk.Label(grid, text=k + ":", foreground="gray").grid(row=i, column=0, sticky="nw", padx=(0, 8))
            ttk.Label(grid, text=v, wraplength=430, justify="left").grid(row=i, column=1, sticky="w")
        ttk.Label(f, text="Arguments:", foreground="gray").pack(anchor="w")
        box = ScrolledText(f, height=10, wrap="word")
        box.insert("1.0", format_arguments(req.arguments))
        box.configure(state="disabled")
        box.pack(fill="both", expand=True, pady=(0, 8))
        self.remember = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text=f"Don’t ask again for “{req.key}” until MCP Guard restarts",
                        variable=self.remember).pack(anchor="w")
        self.countdown = ttk.Label(f, foreground="gray")
        self.countdown.pack(anchor="w", pady=(4, 0))
        row = ttk.Frame(f)
        row.pack(fill="x", pady=(8, 0))
        self.block_btn = ttk.Button(row, text="Block", state="disabled",
                                    command=lambda: self._choose(False, "blocked by user"))
        self.allow_btn = ttk.Button(row, text="Allow", state="disabled",
                                    command=lambda: self._choose(True, "allowed by user"))
        self.block_btn.pack(side="left")
        self.allow_btn.pack(side="right")

        w.lift()
        w.focus_force()
        w.bell()
        w.after(ARM_DELAY_MS, self._arm)
        self._tick()

    def _arm(self) -> None:
        if not self.done:
            self.block_btn.configure(state="normal")
            self.allow_btn.configure(state="normal")
            self.block_btn.focus_set()          # default focus is the safe choice

    def _tick(self) -> None:
        if self.done:
            return
        left = max(0, int(self.deadline - time.time()))
        self.countdown.configure(text=f"Blocks automatically in {left}s if you don’t decide.")
        self.win.after(500, self._tick)

    def _choose(self, allow: bool, why: str) -> None:
        self.finish(Decision(allow, why, "user", remember=bool(self.remember.get())))

    def finish(self, d: Decision) -> None:
        if self.done:
            return
        self.resolve(d)
        self.close()
        self.registry.pop(self.req.id, None)

    def close(self) -> None:
        self.done = True
        try:
            self.win.destroy()
        except Exception:
            pass
