"""Simulate a user testing the bridge end-to-end.

Doesn't touch Slack — uses a FakeApp + the real `make_handler()` from
src/slack_bridge/main.py against real tmux + real gemini. Records what the
bot would have posted to Slack and prints it as if you'd watched it scroll
in the channel.

Each step is wrapped in strict assertions: per-channel routing, exact
session-count progression, exact forwarded-text matches, chrome stripping,
archive verification, and tmux-orphan cleanup. The final summary is a
hard PASS/FAIL with non-zero exit on any failure.

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


class Results:
    """Record strict pass/fail with optional detail strings.

    `check` returns the boolean so callers can short-circuit dependent
    assertions. `summary` prints a single, hard-to-misread banner.
    """

    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.failures = []  # list of (name, detail) for the final summary

    def check(self, name, condition, detail=""):
        if condition:
            print(f"  ✅ PASS  {name}")
            self.passed += 1
            return True
        print(f"  ❌ FAIL  {name}")
        if detail:
            print(f"          {detail}")
        self.failed += 1
        self.failures.append((name, detail))
        return False

    def summary(self):
        total = self.passed + self.failed
        ok = self.failed == 0
        verdict = "PASS" if ok else "FAIL"
        line = "═" * 70
        print(f"\n{line}")
        print(f" RESULT: {verdict}    ({self.passed}/{total} checks passed, "
              f"{self.failed} failed)")
        print(line)
        if not ok:
            print(" Failed checks:")
            for name, detail in self.failures:
                print(f"   - {name}")
                if detail:
                    print(f"       {detail}")
            print(line)
        return ok


results = Results()


def banner(s):
    print(f"\n{BAR}\n## {s}\n{BAR}")


def render(app, since=(0, 0), channel=None):
    """Render new posts/updates since the previous (post_idx, update_idx).

    Returns ((new_snapshot), [texts_for_filter_channel]).

    `channel` filters the returned texts to a single channel id (so step
    assertions can verify *where* a message landed, not just that it
    exists somewhere). The snapshot indices always advance over the full
    posts/updates lists so the caller's diffing stays correct.
    """
    posts = app.client.posts[since[0]:]
    updates = app.client.updates[since[1]:]
    by_ts = {}
    order = []
    for p in posts:
        ts = p.get("ts")
        if ts not in by_ts:
            order.append(ts)
        by_ts[ts] = (p.get("text", ""), p.get("channel"), False)
    for u in updates:
        ts = u.get("ts")
        if ts in by_ts:
            t, c, _ = by_ts[ts]
            by_ts[ts] = (u.get("text", ""), c, True)

    filtered = []
    for ts in order:
        text, ch, edited = by_ts[ts]
        if channel is not None and ch != channel:
            continue
        tag = "[edited ]" if edited else "[posted ]"
        snippet = text if len(text) < 600 else text[:580] + f"\n  ... [+{len(text) - 580} more bytes]"
        print(f"{tag} ch={ch}\n  " + snippet.replace("\n", "\n  "))
        print()
        filtered.append(text)

    return (len(app.client.posts), len(app.client.updates)), filtered


def posts_in(app, channel, since_idx):
    """All *visible* texts posted to `channel` at or after `since_idx`.

    Many bridge replies are a two-step dance: post a "Thinking…"
    placeholder via `chat_postMessage`, then overwrite it with the real
    reply via `chat_update`. A strict assertion has to see what the user
    would actually see, so we apply later updates onto matching ts's
    before returning.
    """
    visible = []
    for p in app.client.posts[since_idx:]:
        if p.get("channel") != channel:
            continue
        ts = p.get("ts")
        text = p.get("text", "")
        # Apply the latest matching update for this ts, if any.
        for u in app.client.updates:
            if u.get("channel") == channel and u.get("ts") == ts:
                text = u.get("text", "")
        visible.append(text)
    return visible


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


def list_named_tmux(prefix):
    """Return tmux session names beginning with `prefix` (empty list if none)."""
    try:
        out = subprocess.check_output(["tmux", "ls", "-F", "#S"], text=True,
                                      stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        return []
    return [s for s in out.strip().split("\n") if s.startswith(prefix)]


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

    # Sanity precondition: starting clean.
    results.check("Pre: bridge.sessions starts empty",
                  len(bridge.sessions) == 0,
                  detail=f"got {dict(bridge.sessions)}")

    # ─────────────────────────────────────────────────────────────────
    banner("STEP 0 — !sessions before any session exists")
    # ─────────────────────────────────────────────────────────────────
    pre_idx = len(app.client.posts)
    handle(USER, DM, "!sessions", None)
    snap, dm_texts = render(app, snap, channel=DM)
    dm_new = posts_in(app, DM, pre_idx)
    results.check("Step 0: exactly one reply in DM",
                  len(dm_new) == 1,
                  detail=f"got {len(dm_new)} posts to DM")
    results.check("Step 0: reply equals 'No active sessions.'",
                  dm_new == ["No active sessions."],
                  detail=f"got {dm_new!r}")
    results.check("Step 0: bridge.sessions still empty",
                  len(bridge.sessions) == 0)

    # ─────────────────────────────────────────────────────────────────
    banner("STEP 1 — race fix: !sessions during a !gemini named startup")
    # ─────────────────────────────────────────────────────────────────
    sim_cwd = str(ROOT)
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
    pre_idx = len(app.client.posts)
    handle(USER, DM, "!sessions", None)
    snap, dm_texts = render(app, snap, channel=DM)
    dm_new = posts_in(app, DM, pre_idx)
    results.check("Step 1: !sessions replies once into the DM (not the agent ch)",
                  len(dm_new) == 1,
                  detail=f"got {len(dm_new)} posts to DM")
    sessions_reply = dm_new[0] if dm_new else ""
    results.check("Step 1: reply header is '*1 active session(s):*'",
                  sessions_reply.startswith("*1 active session(s):*"),
                  detail=f"got {sessions_reply[:120]!r}")
    results.check("Step 1: reply mentions cli `gemini`",
                  "`gemini`" in sessions_reply,
                  detail=f"got {sessions_reply[:200]!r}")
    results.check("Step 1: reply mentions the cwd we passed",
                  sim_cwd in sessions_reply,
                  detail=f"cwd={sim_cwd!r} not found in reply")
    results.check("Step 1: bridge.sessions has exactly one entry mid-boot",
                  len(bridge.sessions) == 1,
                  detail=f"got {dict(bridge.sessions)}")

    # Let the boot finish so we don't strand a tmux session.
    t.join(timeout=120)
    results.check("Step 1: boot thread finished within 120s", not t.is_alive())
    results.check("Step 1: gemini boot raised no exception",
                  not boot_err,
                  detail=repr(boot_err[0]) if boot_err else "")
    print("[gemini boot finished — `!sessions` above should have already shown the session]\n")
    snap, _ = render(app, snap)

    # Identify the named channel id we created.
    named_channel_ids = [cid for cid, _ in app.client.created_channels
                         if cid in bridge.sessions]
    named_ch = named_channel_ids[0] if named_channel_ids else None
    print(f"[named channel id = {named_ch}]")
    results.check("Step 1: exactly one Slack channel was created",
                  len(app.client.created_channels) == 1,
                  detail=f"got {[c[0] for c in app.client.created_channels]}")
    results.check("Step 1: created channel is registered in bridge.sessions",
                  named_ch is not None,
                  detail=f"sessions={list(bridge.sessions)}, "
                         f"created={[c[0] for c in app.client.created_channels]}")

    # ─────────────────────────────────────────────────────────────────
    banner("STEP 2 — typo guard: !session (singular) must NOT be forwarded")
    # ─────────────────────────────────────────────────────────────────
    if named_ch:
        pre_idx = len(app.client.posts)
        with mock.patch.object(bridge, "send_and_wait") as saw:
            handle(USER, named_ch, "!session", None)
        results.check("Step 2: send_and_wait was NOT called for !session typo",
                      saw.call_count == 0,
                      detail=f"call_count={saw.call_count}")
        snap, _ = render(app, snap, channel=named_ch)
        ch_new = posts_in(app, named_ch, pre_idx)
        results.check("Step 2: exactly one error reply in the agent channel",
                      len(ch_new) == 1,
                      detail=f"got {len(ch_new)} posts to {named_ch}")
        body = ch_new[0] if ch_new else ""
        results.check("Step 2: error names the bad command exactly",
                      "Unknown bridge command `!session`" in body,
                      detail=f"got {body[:160]!r}")
        results.check("Step 2: error suggests `!help`",
                      "!help" in body,
                      detail=f"got {body[:160]!r}")

    # ─────────────────────────────────────────────────────────────────
    banner("STEP 3 — mid-text bang IS forwarded (not intercepted)")
    # ─────────────────────────────────────────────────────────────────
    if named_ch:
        msg = "fix the !important flag"
        pre_idx = len(app.client.posts)
        with mock.patch.object(bridge, "send_and_wait", return_value="ack from agent") as saw:
            handle(USER, named_ch, msg, None)
        results.check("Step 3: send_and_wait was called exactly once",
                      saw.call_count == 1,
                      detail=f"call_count={saw.call_count}")
        # Verify the *exact* user text was forwarded — the bug we're
        # guarding against is the bridge mangling or dropping the
        # message before sending. send_and_wait is called positionally
        # with (tmux_name, text, ...), so probe both args and kwargs.
        forwarded = ""
        if saw.call_args is not None:
            args, kwargs = saw.call_args
            forwarded = (args[1] if len(args) > 1 else
                         kwargs.get("text", kwargs.get("message", "")))
        results.check("Step 3: forwarded text matches the user's exact message",
                      forwarded == msg,
                      detail=f"forwarded={forwarded!r}, expected={msg!r}")
        snap, _ = render(app, snap, channel=named_ch)
        ch_new = posts_in(app, named_ch, pre_idx)
        results.check("Step 3: agent reply 'ack from agent' shown in agent ch",
                      any("ack from agent" in t for t in ch_new),
                      detail=f"posts={ch_new!r}")

    # ─────────────────────────────────────────────────────────────────
    banner("STEP 4 — empty-extraction fallback shows recent activity, not chrome")
    # ─────────────────────────────────────────────────────────────────
    if named_ch:
        gemini_chrome = (
            " > what's the progress?\n"
            "                                                          ? for shortcuts\n"
            "  shell mode enabled (esc to disable)\n"
            "▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄\n"
            " !   Type your shell command\n"
            " workspace (/directory)        branch    sandbox    /model    quota\n"
            " /home/user/code/example      main      no sandbox Auto (Gemini 3)  2% used\n"
            "real progress: writing rename_script.py (line 47/120)\n"
        )
        pre_idx = len(app.client.posts)
        with mock.patch.object(bridge, "send_and_wait", return_value=""), \
             mock.patch.object(bridge, "extract_pending_dialog", return_value=""), \
             mock.patch.object(bridge, "capture", return_value=gemini_chrome):
            handle(USER, named_ch, "what's the progress?", None)
        snap, _ = render(app, snap, channel=named_ch)
        ch_new = posts_in(app, named_ch, pre_idx)
        joined = "\n".join(ch_new)
        results.check("Step 4: at least one fallback post appeared",
                      len(ch_new) >= 1,
                      detail=f"got {len(ch_new)} posts to {named_ch}")
        results.check("Step 4: fallback contains the real activity line",
                      "real progress: writing rename_script.py" in joined,
                      detail=f"posts={ch_new!r}")
        # Each chrome marker is checked individually so a single
        # surviving line can't be hidden behind a passing aggregate.
        for marker, label in [
            ("▄", "box-drawing rule"),
            ("? for shortcuts", "shortcuts hint"),
            ("shell mode enabled", "shell-mode banner"),
            ("workspace (/directory)", "header row"),
            ("Type your shell command", "shell prompt label"),
        ]:
            results.check(f"Step 4: chrome marker stripped — {label}",
                          marker not in joined,
                          detail=f"marker {marker!r} leaked into post")

    # ─────────────────────────────────────────────────────────────────
    banner("STEP 5 — !cancel + !end to clean up")
    # ─────────────────────────────────────────────────────────────────
    if named_ch:
        archived_before = list(app.client.archived)
        pre_idx = len(app.client.posts)
        handle(USER, named_ch, "!cancel", None)
        handle(USER, named_ch, "!end", None)
        snap, _ = render(app, snap, channel=named_ch)
        ch_new = posts_in(app, named_ch, pre_idx)
        joined = "\n".join(ch_new)
        results.check("Step 5: !cancel acked with 'Sent interrupt to `gemini`'",
                      "Sent interrupt to `gemini`" in joined,
                      detail=f"posts={ch_new!r}")
        results.check("Step 5: !end posted 'Session ended'",
                      "Session ended" in joined,
                      detail=f"posts={ch_new!r}")
        results.check("Step 5: named channel was archived",
                      named_ch in app.client.archived
                      and named_ch not in archived_before,
                      detail=f"archived={app.client.archived!r}")
        results.check("Step 5: bridge.sessions no longer holds the named ch",
                      named_ch not in bridge.sessions,
                      detail=f"sessions={list(bridge.sessions)}")

    # ─────────────────────────────────────────────────────────────────
    banner("DONE")
    # ─────────────────────────────────────────────────────────────────
    print(f"sessions at end: {dict(bridge.sessions)}")
    results.check("Final: no in-memory sessions remaining",
                  len(bridge.sessions) == 0,
                  detail=f"got {dict(bridge.sessions)}")

    # Be polite — kill only OUR tmux sessions, leave real bridge alone.
    sim_prefix = f"gemini-{SIM_PROJECT_NAME}-"
    kill_named_tmux(sim_prefix)
    leftover = list_named_tmux(sim_prefix)
    results.check("Final: no leftover sim tmux sessions after cleanup",
                  not leftover,
                  detail=f"leftover={leftover!r}")

    if not results.summary():
        sys.exit(1)


if __name__ == "__main__":
    main()
