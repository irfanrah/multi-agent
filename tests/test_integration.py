"""Integration tests against real tmux + real CLIs (gemini, codex).

Exercises the same primitives the Slack handlers call: start_session,
send_and_wait, kill_session, the cancel/switch/model flows. Each test launches
a real CLI, sends one short prompt, and shuts down — so they consume a small
amount of real API quota. Auto-skips if a CLI is missing or unauthenticated.

Run:  python3 -m unittest tests.test_integration -v
Run one:  python3 -m unittest tests.test_integration.GeminiSmokeTests.test_roundtrip -v
"""
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "slack_bridge"))

import main as bridge  # noqa: E402


PID = os.getpid()
START_BUDGET = 90  # seconds — snap CLIs are slow under load
TURN_BUDGET = 90   # seconds — short prompt should round-trip well under this


def _have(cli):
    return shutil.which(cli) is not None


def _starts_clean(cli, name, **kw):
    """Return (ok, pane_text). Used by setUpClass to detect unauthenticated CLIs."""
    try:
        ok = bridge.start_session(cli, name, max_wait=START_BUDGET, **kw)
        text = bridge.capture(name)
        return ok, text
    finally:
        bridge.kill_session(name)


def _looks_unauthenticated(text):
    needles = ("Sign in", "sign-in", "log in", "login", "authentication required",
               "authorize", "auth", "api key not")
    low = text.lower()
    # Heuristic: if the pane mentions auth flows AND lacks the ready prompt
    # markers, treat it as un-authenticated.
    if any(n.lower() in low for n in needles) and "type your message" not in low and "›" not in text:
        return True
    return False


# ---- gemini ------------------------------------------------------------------

@unittest.skipUnless(_have("gemini"), "gemini CLI not on PATH")
class GeminiSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ok, text = _starts_clean("gemini", f"it_gemini_probe_{PID}")
        if not ok or _looks_unauthenticated(text):
            raise unittest.SkipTest(
                "gemini CLI did not reach ready prompt; likely not authenticated."
            )

    def setUp(self):
        self.name = f"it_gemini_{self._testMethodName}_{PID}"

    def tearDown(self):
        bridge.kill_session(self.name)

    def test_roundtrip(self):
        self.assertTrue(
            bridge.start_session("gemini", self.name, max_wait=START_BUDGET),
            "gemini did not reach ready prompt",
        )
        # One short prompt; the CLI's reply should appear within budget.
        reply = bridge.send_and_wait(
            self.name, "Reply with exactly the single word READY.",
            "gemini", max_wait=TURN_BUDGET,
        )
        self.assertIn("READY", reply.upper(),
                      f"expected READY in reply, got: {reply!r}")

    def test_cancel_returns_to_prompt(self):
        self.assertTrue(bridge.start_session("gemini", self.name, max_wait=START_BUDGET))
        # Kick off a long-form prompt without waiting for completion.
        bridge._tmux("send-keys", "-t", self.name, "-l",
                     "Write a 5000 word essay about the history of the abacus.")
        bridge._tmux("send-keys", "-t", self.name, "Enter")
        time.sleep(3)  # let the CLI start responding / show "Thinking…"
        # Send Esc — same keystroke !cancel sends.
        bridge._tmux("send-keys", "-t", self.name, "Escape")
        time.sleep(2)
        # CLI must still be alive and not stuck in "Thinking…" — the cancel
        # signal succeeded if both hold. We can't assert the input placeholder
        # is back because gemini retains the user's typed text after cancel.
        self.assertTrue(bridge.session_exists(self.name),
                        "Esc must not kill the gemini process")
        text = bridge.capture(self.name)
        self.assertNotIn("Thinking", text,
                         f"gemini still appears to be thinking after Esc:\n{text[-400:]}")

    def test_model_flag_launches(self):
        # Use a known-good gemini model name.
        ok = bridge.start_session(
            "gemini", self.name,
            max_wait=START_BUDGET,
            extra_args="-m gemini-2.5-flash",
        )
        self.assertTrue(ok, "gemini -m gemini-2.5-flash did not reach ready prompt")


# ---- codex -------------------------------------------------------------------

@unittest.skipUnless(_have("codex"), "codex CLI not on PATH")
class CodexSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ok, text = _starts_clean("codex", f"it_codex_probe_{PID}")
        if not ok or _looks_unauthenticated(text):
            raise unittest.SkipTest(
                "codex CLI did not reach ready prompt; likely not authenticated."
            )

    def setUp(self):
        self.name = f"it_codex_{self._testMethodName}_{PID}"

    def tearDown(self):
        bridge.kill_session(self.name)

    def test_roundtrip(self):
        self.assertTrue(
            bridge.start_session("codex", self.name, max_wait=START_BUDGET),
            "codex did not reach ready prompt",
        )
        reply = bridge.send_and_wait(
            self.name, "Reply with the single word READY only.",
            "codex", max_wait=TURN_BUDGET,
        )
        self.assertIn("READY", reply.upper(),
                      f"expected READY in reply, got: {reply!r}")

    def test_cancel_returns_to_prompt(self):
        self.assertTrue(bridge.start_session("codex", self.name, max_wait=START_BUDGET))
        bridge._tmux("send-keys", "-t", self.name, "-l",
                     "Write a 5000 word essay about FORTRAN.")
        bridge._tmux("send-keys", "-t", self.name, "Enter")
        time.sleep(3)
        bridge._tmux("send-keys", "-t", self.name, "Escape")
        time.sleep(2)
        self.assertTrue(bridge.session_exists(self.name),
                        "Esc must not kill the codex process")
        text = bridge.capture(self.name)
        self.assertNotIn("Thinking", text,
                         f"codex still appears to be thinking after Esc:\n{text[-400:]}")
        # Ready marker `›` is present whether at idle or with text in the input.
        self.assertIn(bridge.CLI_CONFIGS["codex"]["ready_marker"], text)


# ---- switch (gemini → codex on same tmux name) -----------------------------

@unittest.skipUnless(_have("gemini") and _have("codex"),
                     "both gemini and codex required for switch test")
class SwitchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ok_g, t_g = _starts_clean("gemini", f"it_switch_probe_g_{PID}")
        ok_c, t_c = _starts_clean("codex",  f"it_switch_probe_c_{PID}")
        if not (ok_g and ok_c) or _looks_unauthenticated(t_g) or _looks_unauthenticated(t_c):
            raise unittest.SkipTest(
                "one of gemini/codex did not reach ready prompt; skipping switch test"
            )

    def setUp(self):
        self.name = f"it_switch_{PID}"

    def tearDown(self):
        bridge.kill_session(self.name)

    def test_gemini_then_codex_same_name(self):
        # 1. Start gemini.
        self.assertTrue(bridge.start_session("gemini", self.name, max_wait=START_BUDGET))
        text = bridge.capture(self.name)
        self.assertIn(bridge.CLI_CONFIGS["gemini"]["ready_marker"], text)
        # 2. Kill + relaunch as codex (mirrors what !switch does).
        bridge.kill_session(self.name)
        self.assertTrue(bridge.start_session("codex", self.name, max_wait=START_BUDGET))
        text2 = bridge.capture(self.name)
        self.assertIn(bridge.CLI_CONFIGS["codex"]["ready_marker"], text2)
        self.assertNotIn("Type your message", text2,
                         "old gemini state should be gone after switch")


if __name__ == "__main__":
    unittest.main()
