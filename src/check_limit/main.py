import os
import subprocess
import time
import re

BOX_CHARS = r"[│╭╯╰╮─█░▝▘▛▜▟▞▖▗▎▏▬·]"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUTPUT_DIR = os.path.join(REPO_ROOT, "output", "check_limit")

def capture_cli_usage(command, session_name="usage_check", send_keys=None,
                      startup_wait=10, post_wait=4, *,
                      ready_check=None, prompt_ready=None,
                      max_wait=40, poll=1.0, send_settle=1.5):
    """Run a command in tmux, optionally type a slash command, capture the UI, kill the session.

    Two modes:
    - Legacy (fixed sleeps): if `ready_check` is None, sleep `startup_wait`, optionally
      send_keys, sleep `post_wait`, capture once.
    - Polling: if `ready_check(text) -> bool` is provided, poll capture every `poll`
      seconds until ready_check returns True or `max_wait` elapses. If `prompt_ready`
      is provided alongside `send_keys`, wait for prompt_ready(text) to be True before
      typing — handy for TUIs that need a moment before accepting input.

    Polling is more robust against slow startups (update banners, MOTDs, login checks).
    """
    # `claude /usage` and `gemini /model` are one-shot: they print their
    # panel and exit. By default tmux destroys a session as soon as its
    # last command exits, so capture-pane would race against the session
    # disappearing. Wrap the command with a long-running tail so the pane
    # sticks around with the rendered output until our `finally` kills it.
    keepalive = max(int(max_wait) + 30, 90)
    wrapped = f"{command}; sleep {keepalive}"

    def _capture():
        # capture-pane prints "can't find pane: <name>" to stderr if the
        # session vanished or hasn't materialized yet. Treat that as a
        # transient empty read; the polling loop will retry.
        try:
            return subprocess.check_output(
                ["tmux", "capture-pane", "-pt", session_name],
                text=True, stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            return ""

    try:
        subprocess.run(["tmux", "kill-session", "-t", session_name],
                       stderr=subprocess.DEVNULL)
        subprocess.run(["tmux", "new-session", "-d", "-s", session_name,
                        "-x", "200", "-y", "50", wrapped],
                       stderr=subprocess.DEVNULL)
        # Sanity check: if the session never came up (rare, but tmux server
        # contention can do it), return early with a clear error rather
        # than spinning the polling loop on a missing pane.
        if subprocess.run(["tmux", "has-session", "-t", session_name],
                          stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode != 0:
            return f"Error: tmux session {session_name} failed to start"

        if ready_check is None:
            # Legacy fixed-time path.
            time.sleep(startup_wait)
            if send_keys:
                subprocess.run(["tmux", "send-keys", "-t", session_name, "-l", send_keys])
                time.sleep(send_settle)
                subprocess.run(["tmux", "send-keys", "-t", session_name, "Enter"])
                time.sleep(post_wait)
            return _capture()

        # Polling path.
        sent = (send_keys is None)
        start = time.time()
        text = ""
        while time.time() - start < max_wait:
            text = _capture()
            if not sent:
                if prompt_ready is None or prompt_ready(text):
                    subprocess.run(["tmux", "send-keys", "-t", session_name, "-l", send_keys])
                    time.sleep(send_settle)
                    subprocess.run(["tmux", "send-keys", "-t", session_name, "Enter"])
                    sent = True
            elif ready_check(text):
                return text
            time.sleep(poll)
        return text
    except Exception as e:
        return f"Error: {e}"
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session_name], stderr=subprocess.DEVNULL)

def _clean(line):
    return re.sub(r"\s+", " ", re.sub(BOX_CHARS, " ", line)).strip()

def parse_claude_rows(text):
    """Claude /usage: label line, then bar + N% used, then 'Resets ...'.
    Returns [{"label": str, "pct_used": int, "reset": str|None}, ...]."""
    lines = [_clean(l) for l in text.split("\n")]
    labels = {"Current session", "Current week (all models)", "Current week (Sonnet only)"}
    rows, current = [], None
    for i, line in enumerate(lines):
        if line in labels:
            current = line
            continue
        m = re.search(r"(\d+)%\s*used", line)
        if m and current:
            pct = int(m.group(1))
            reset = None
            if i + 1 < len(lines):
                rm = re.match(r"Resets\s+(.+)", lines[i + 1])
                if rm:
                    reset = rm.group(1).strip()
            rows.append({"label": current, "pct_used": pct, "reset": reset})
            current = None
    return rows

def parse_gemini_rows(text):
    """Gemini /model: 'Flash <bar> 24% Resets: 2:49 PM (21h 29m)'."""
    rows = []
    for line in text.split("\n"):
        c = _clean(line)
        m = re.match(r"^(Flash Lite|Flash|Pro)\s+(\d+)%(?:\s*Resets?:?\s*(.+))?$", c)
        if m:
            rows.append({
                "label": m.group(1),
                "pct_used": int(m.group(2)),
                "reset": m.group(3).strip() if m.group(3) else None,
            })
    return rows

def parse_codex_rows(text):
    """Codex /status panel: '5h limit: [bar] 49% left (resets 19:28)' etc."""
    rows = []
    for line in text.split("\n"):
        c = _clean(line)
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9 ]*?limit):.*?(\d+)%\s*(used|left)\s*(?:\(resets?\s+([^)]+)\))?",
                     c, re.IGNORECASE)
        if m:
            label, val, kind, reset = m.group(1).strip(), int(m.group(2)), m.group(3).lower(), m.group(4)
            used = 100 - val if kind == "left" else val
            rows.append({"label": label, "pct_used": used, "reset": reset.strip() if reset else None})
    if rows:
        return rows
    # Fallback: footer-only line "gpt-5.4 default · 100% left · /path" (· stripped to space).
    for line in text.split("\n"):
        c = _clean(line)
        m = re.match(r"^(\S+)\s+(\S+)\s+(\d+)%\s*(used|left)\b", c)
        if m:
            model, plan, val, kind = m.group(1), m.group(2), int(m.group(3)), m.group(4).lower()
            used = 100 - val if kind == "left" else val
            return [{"label": f"{model} ({plan})", "pct_used": used, "reset": None}]
    return []

def format_rows(rows):
    if not rows:
        return "Usage not found"
    parts = []
    for r in rows:
        s = f"{r['label']}: {r['pct_used']}% used"
        if r.get("reset"):
            s += f" (resets {r['reset']})"
        parts.append(s)
    return " | ".join(parts)

def parse_claude(text): return format_rows(parse_claude_rows(text))
def parse_gemini(text): return format_rows(parse_gemini_rows(text))
def parse_codex(text):  return format_rows(parse_codex_rows(text))

PARSERS = {"Claude": parse_claude, "Gemini": parse_gemini, "Codex": parse_codex}
ROW_PARSERS = {"Claude": parse_claude_rows, "Gemini": parse_gemini_rows, "Codex": parse_codex_rows}

if __name__ == "__main__":
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    # For Codex, slash commands only work interactively, so launch the REPL and send /status.
    clis = {
        "Claude": {"cmd": "claude /usage"},
        "Gemini": {"cmd": "gemini /model"},
        "Codex":  {"cmd": "codex", "send_keys": "/status", "post_wait": 10},
    }
    print("=== AI CLI USAGE SUMMARY (via TMUX) ===")
    for name, opts in clis.items():
        print(f"Checking {name}...")
        raw = capture_cli_usage(
            opts["cmd"], f"check_{name.lower()}",
            send_keys=opts.get("send_keys"),
            post_wait=opts.get("post_wait", 4),
        )
        out_path = os.path.join(OUTPUT_DIR, f"{name.lower()}_output.txt")
        with open(out_path, "w") as f:
            f.write(raw)
        summary = PARSERS[name](raw)
        print(f"  Summary: {summary}")
        print(f"  (Raw saved to {os.path.relpath(out_path, REPO_ROOT)})\n")
