"""Simulate a user testing the bridge end-to-end.

Doesn't touch Slack — uses a FakeApp + the real `make_handler()` from
src/slack_bridge/main.py against real tmux + real gemini. Records what the
bot would have posted to Slack and prints it as if you'd watched it scroll
in the channel.

Run: python3 tests/sim_user.py
"""
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "slack_bridge"))
sys.path.insert(0, str(ROOT / "tests"))

import main as bridge  # noqa: E402
from test_handlers import FakeApp  # noqa: E402

BAR = "─" * 70
SIM_PROJECT_NAME = "sim_test"


def banner(s):
    print(f"\n{BAR}\n## {s}\n{BAR}")


def render(app, since=(0, 0), channel=None):
    """Render new posts/updates since the previous (post_idx, update_idx).

    Returns the new (post_idx, update_idx) snapshot so caller can diff
    only what's new on the next call.
    """
    posts = app.client.posts[since[0]:]
    updates = app.client.updates[since[1]:]
    # Build ts→latest text map so chat.update overwrites are reflected.
    by_ts = {}
    order = []
    for p in posts:
        if channel is not None and p.get("channel") != channel:
            continue
        ts = p.get("ts")
        if ts not in by_ts:
            order.append(ts)
        by_ts[ts] = (p.get("text", ""), p.get("channel"), False)
    for u in updates:
        if channel is not None and u.get("channel") != channel:
            continue
        ts = u.get("ts")
        if ts in by_ts:
            t, c, _ = by_ts[ts]
            by_ts[ts] = (u.get("text", ""), c, True)
    for ts in order:
        text, ch, edited = by_ts[ts]
        tag = "[edited ]" if edited else "[posted ]"
        snippet = text if len(text) < 600 else text[:580] + f"\n  ... [+{len(text) - 580} more bytes]"
        print(f"{tag} ch={ch}\n  " + snippet.replace("\n", "\n  "))
        print()
    return (len(app.client.posts), len(app.client.updates))


def kill_named_tmux(prefix):
    """Kill only tmux sessions matching our sim prefix — leave the real
    bridge's sessions alone."""
    try:
        out = subprocess.check_output(["tmux", "ls", "-F", "#S"], text=True,
                                      stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        return  # no tmux server
    for sess_name in out.strip().split("\n"):
        if sess_name.startswith(prefix):
            subprocess.run(["tmux", "kill-session", "-t", sess_name],
                           stderr=subprocess.DEVNULL)


def main():
    # Isolate from any state in the running bridge process; this is a
    # different python process so it doesn't actually share state.
    bridge.sessions.clear()
    bridge.pending_uploads.clear()

    app = FakeApp()
    handle = bridge.make_handler(app)

    USER = "U_SIM"
    DM = "D_SIM"
    snap = (0, 0)

    # ─────────────────────────────────────────────────────────────────
    banner("STEP 0 — !sessions before any session exists")
    # ─────────────────────────────────────────────────────────────────
    handle(USER, DM, "!sessions", None)
    snap = render(app, snap)

    # ─────────────────────────────────────────────────────────────────
    banner("STEP 1 — race fix: !sessions during a !gemini named startup")
    # ─────────────────────────────────────────────────────────────────
    # The bug from the transcript: user types !sessions in the agent channel
    # right after the channel is created but BEFORE gemini's tmux finishes
    # booting (~20s). With the fix, !sessions immediately sees the in-
    # progress session. Without the fix, it returned "No active sessions."
    sim_cwd = str(ROOT)  # any real, accessible directory
    boot_err = []

    def kick_named():
        try:
            handle(USER, DM, f"!gemini {SIM_PROJECT_NAME} {sim_cwd}", None)
        except Exception as e:
            boot_err.append(e)

    t = threading.Thread(target=kick_named, daemon=True)
    t.start()
    # Wait long enough for start_named_session to register the session
    # placeholder, but well before tmux is up.
    time.sleep(2)
    print("[user types `!sessions` ~2s into the gemini boot]\n")
    handle(USER, DM, "!sessions", None)
    snap = render(app, snap)
    # Let the boot finish so we don't strand a tmux session.
    t.join(timeout=120)
    if boot_err:
        print(f"!! boot raised {boot_err[0]!r}")
    print("[gemini boot finished — `!sessions` above should have already shown the session]\n")
    snap = render(app, snap)

    # Identify the named channel id we created (FakeClient picks C0000, C0001, …).
    named_channel_ids = [cid for cid, _ in app.client.created_channels
                         if cid in bridge.sessions]
    named_ch = named_channel_ids[0] if named_channel_ids else None
    print(f"[named channel id = {named_ch}]")

    # ─────────────────────────────────────────────────────────────────
    banner("STEP 2 — typo guard: !session (singular) must NOT be forwarded")
    # ─────────────────────────────────────────────────────────────────
    if named_ch is None:
        print("(skipped: no named session)")
    else:
        with mock.patch.object(bridge, "send_and_wait") as saw:
            handle(USER, named_ch, "!session", None)
        if saw.called:
            print("!! BUG: send_and_wait was called — bridge forwarded the typo")
        else:
            print("[good: send_and_wait was NOT called — bridge intercepted]\n")
        snap = render(app, snap)

    # ─────────────────────────────────────────────────────────────────
    banner("STEP 3 — mid-text bang IS forwarded (not intercepted)")
    # ─────────────────────────────────────────────────────────────────
    if named_ch is None:
        print("(skipped: no named session)")
    else:
        with mock.patch.object(bridge, "send_and_wait", return_value="ack from agent"):
            handle(USER, named_ch, "fix the !important flag", None)
        snap = render(app, snap)

    # ─────────────────────────────────────────────────────────────────
    banner("STEP 4 — empty-extraction fallback shows recent activity, not chrome")
    # ─────────────────────────────────────────────────────────────────
    # Simulate the situation that produced the wall of "▄▄▄ workspace
    # (/directory) Auto (Gemini 3) 2% used" chrome before the fix.
    if named_ch is None:
        print("(skipped: no named session)")
    else:
        gemini_chrome = (
            " > what's the progress?\n"
            "                                                          ? for shortcuts\n"
            "  shell mode enabled (esc to disable)\n"
            "▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄\n"
            " !   Type your shell command\n"
            " workspace (/directory)        branch    sandbox    /model    quota\n"
            " /home/kurnianto/code/CCTV     main      no sandbox Auto (Gemini 3)  2% used\n"
            "real progress: writing rename_script.py (line 47/120)\n"
        )
        with mock.patch.object(bridge, "send_and_wait", return_value=""), \
             mock.patch.object(bridge, "extract_pending_dialog", return_value=""), \
             mock.patch.object(bridge, "capture", return_value=gemini_chrome):
            handle(USER, named_ch, "what's the progress?", None)
        snap = render(app, snap)

    # ─────────────────────────────────────────────────────────────────
    banner("STEP 5 — !cancel + !end to clean up")
    # ─────────────────────────────────────────────────────────────────
    if named_ch is not None:
        handle(USER, named_ch, "!cancel", None)
        handle(USER, named_ch, "!end", None)
        snap = render(app, snap)

    # ─────────────────────────────────────────────────────────────────
    banner("DONE")
    # ─────────────────────────────────────────────────────────────────
    print(f"sessions at end: {dict(bridge.sessions)}")
    # Be polite — kill only OUR tmux sessions, leave real bridge alone.
    kill_named_tmux(f"gemini-{SIM_PROJECT_NAME}-")


if __name__ == "__main__":
    main()
