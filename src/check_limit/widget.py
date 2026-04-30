"""Ubuntu desktop widget that visualizes AI CLI usage limits.

Run: python3 src/check_limit/widget.py
Refreshes every 60 seconds. Captures Claude / Gemini / Codex in parallel.
"""
import datetime as dt
import os
import sys
import threading
import tkinter as tk

# Allow running the script directly from anywhere.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from main import (  # noqa: E402
    capture_cli_usage,
    parse_claude_rows,
    parse_gemini_rows,
    parse_codex_rows,
)

# Expected minimum row counts — used to decide when polling is "done" so we
# don't return on a partially rendered panel.
CLIS = [
    ("Claude", {
        "cmd": "claude /usage",
        "parser": parse_claude_rows,
        "expected": 3,  # session, week-all, week-sonnet
    }),
    ("Gemini", {
        "cmd": "gemini /model",
        "parser": parse_gemini_rows,
        "expected": 3,  # Flash, Flash Lite, Pro
    }),
    ("Codex", {
        "cmd": "codex",
        "send_keys": "/status",
        # Codex's TUI shows '›' once it's ready to accept input.
        "prompt_ready": lambda t: "›" in t,
        "parser": parse_codex_rows,
        "expected": 2,  # 5h, weekly
    }),
]
REFRESH_MS = 60_000
MAX_WAIT_SEC = 45


def _capture_one(name, opts):
    parser = opts["parser"]
    expected = opts.get("expected", 1)
    raw = capture_cli_usage(
        opts["cmd"], f"widget_{name.lower()}",
        send_keys=opts.get("send_keys"),
        prompt_ready=opts.get("prompt_ready"),
        ready_check=lambda t: len(parser(t)) >= expected,
        max_wait=MAX_WAIT_SEC,
    )
    return name, parser(raw)


def fetch_all_parallel():
    """Run the three captures concurrently — they use distinct tmux sessions."""
    results = {}
    threads = []
    lock = threading.Lock()

    def worker(name, opts):
        try:
            n, rows = _capture_one(name, opts)
            with lock:
                results[n] = rows
        except Exception as e:
            with lock:
                results[name] = [{"label": f"error: {e}", "pct_used": 0, "reset": None}]

    for name, opts in CLIS:
        t = threading.Thread(target=worker, args=(name, opts), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    return results


class UsageWidget(tk.Tk):
    # Catppuccin Mocha palette — easy on the eyes against most Ubuntu themes.
    BG = "#1e1e2e"
    SURFACE = "#313244"
    FG = "#cdd6f4"
    SUB = "#a6adc8"
    ACCENT = "#89b4fa"
    LOW = "#a6e3a1"   # green
    MID = "#f9e2af"   # yellow
    HIGH = "#fab387"  # orange
    CRIT = "#f38ba8"  # red

    def __init__(self):
        super().__init__()
        self.title("AI CLI Usage")
        self.configure(bg=self.BG)
        self.geometry("440x560")
        self.minsize(380, 420)

        self._build_header()
        self.body = tk.Frame(self, bg=self.BG)
        self.body.pack(fill="both", expand=True, padx=14, pady=(2, 4))
        self._build_footer()

        self.refresh()

    # ---- chrome ---------------------------------------------------------
    def _build_header(self):
        bar = tk.Frame(self, bg=self.BG)
        bar.pack(fill="x", padx=14, pady=(14, 6))
        tk.Label(
            bar, text="AI CLI Usage", bg=self.BG, fg=self.FG,
            font=("Ubuntu", 14, "bold"),
        ).pack(side="left")
        self.refresh_btn = tk.Button(
            bar, text="↻ Refresh", bg=self.SURFACE, fg=self.FG,
            activebackground=self.ACCENT, activeforeground=self.BG,
            relief="flat", bd=0, padx=10, pady=4,
            font=("Ubuntu", 10), cursor="hand2", command=self.refresh,
        )
        self.refresh_btn.pack(side="right")

    def _build_footer(self):
        self.status = tk.Label(
            self, text="Loading…", bg=self.BG, fg=self.SUB,
            font=("Ubuntu", 9), anchor="w",
        )
        self.status.pack(side="bottom", fill="x", padx=14, pady=(4, 12))

    # ---- color logic ----------------------------------------------------
    def color_for(self, pct):
        if pct >= 90: return self.CRIT
        if pct >= 75: return self.HIGH
        if pct >= 50: return self.MID
        return self.LOW

    # ---- rendering ------------------------------------------------------
    def render(self, results):
        for w in self.body.winfo_children():
            w.destroy()
        for name, _ in CLIS:
            rows = results.get(name) or []
            self._draw_section(name, rows)

    def _draw_section(self, name, rows):
        section = tk.Frame(self.body, bg=self.BG)
        section.pack(fill="x", pady=(8, 2))
        tk.Label(
            section, text=name.upper(), bg=self.BG, fg=self.ACCENT,
            font=("Ubuntu Mono", 9, "bold"),
        ).pack(anchor="w")
        if not rows:
            tk.Label(
                section, text="(no data — CLI didn't render in time)",
                bg=self.BG, fg=self.CRIT, font=("Ubuntu", 9, "italic"),
            ).pack(anchor="w", pady=(2, 0))
            return
        for r in rows:
            self._draw_row(section, r["label"], r["pct_used"], r.get("reset"))

    def _draw_row(self, parent, label, pct, reset):
        row = tk.Frame(parent, bg=self.BG)
        row.pack(fill="x", pady=4)

        top = tk.Frame(row, bg=self.BG)
        top.pack(fill="x")
        tk.Label(
            top, text=label, bg=self.BG, fg=self.FG, font=("Ubuntu", 10),
        ).pack(side="left")
        tk.Label(
            top, text=f"{pct}%", bg=self.BG, fg=self.color_for(pct),
            font=("Ubuntu Mono", 10, "bold"),
        ).pack(side="right")

        track = tk.Frame(row, bg=self.SURFACE, height=6)
        track.pack(fill="x", pady=(3, 0))
        track.pack_propagate(False)
        if pct > 0:
            fill = tk.Frame(track, bg=self.color_for(pct))
            fill.place(relx=0, rely=0, relwidth=min(pct, 100) / 100, relheight=1)

        if reset:
            tk.Label(
                row, text=f"resets {reset}", bg=self.BG, fg=self.SUB,
                font=("Ubuntu", 8),
            ).pack(anchor="w", pady=(2, 0))

    # ---- refresh cycle --------------------------------------------------
    def refresh(self):
        self.refresh_btn.configure(state="disabled", text="↻ Refreshing…")
        self.status.configure(text="Refreshing… (captures take ~10–15s)")
        threading.Thread(target=self._fetch_in_bg, daemon=True).start()

    def _fetch_in_bg(self):
        try:
            results = fetch_all_parallel()
        except Exception as e:
            self.after(0, lambda: self._on_error(e))
            return
        self.after(0, lambda: self._on_results(results))

    def _on_results(self, results):
        self.render(results)
        ts = dt.datetime.now().strftime("%H:%M:%S")
        self.status.configure(text=f"Updated {ts} · auto-refresh every 60s")
        self.refresh_btn.configure(state="normal", text="↻ Refresh")
        self.after(REFRESH_MS, self.refresh)

    def _on_error(self, e):
        self.status.configure(text=f"Error: {e}")
        self.refresh_btn.configure(state="normal", text="↻ Refresh")
        self.after(REFRESH_MS, self.refresh)


if __name__ == "__main__":
    UsageWidget().mainloop()
