"""Unit tests for slack_bridge command handlers.

Run:  python3 -m unittest tests.test_handlers -v

The bridge is stitched together via closures in `make_handler(app)`, so each
test builds a `FakeApp` (no slack_bolt deps, no tmux, no subprocess) and calls
the returned `handle` directly. Anything that would touch the real world
(`tmux`, `subprocess.run`, `urllib.request.urlopen`, `start_session`, …) is
patched at the slack_bridge module path.
"""
import io
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

# Add src/slack_bridge to path so we can import main as the bridge module.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "slack_bridge"))

import main as bridge  # noqa: E402


# ---- fakes -------------------------------------------------------------------

class FakeClient:
    token = "xoxb-test-token"

    def __init__(self):
        self.posts = []
        self.updates = []
        self.uploads = []
        self.created_channels = []
        self.invited = []
        self.archived = []

    def chat_postMessage(self, **kw):
        self.posts.append(kw)
        return {"ts": str(len(self.posts)), "channel": kw.get("channel")}

    def chat_update(self, **kw):
        self.updates.append(kw)
        return {"ok": True}

    def files_upload_v2(self, **kw):
        self.uploads.append(kw)
        return {"ok": True}

    def conversations_create(self, **kw):
        cid = f"C{len(self.created_channels):04d}"
        self.created_channels.append((cid, kw))
        return {"channel": {"id": cid, "name": kw.get("name")}}

    def conversations_invite(self, **kw):
        self.invited.append(kw)
        return {"ok": True}

    def conversations_setTopic(self, **kw):
        return {"ok": True}

    def conversations_archive(self, **kw):
        self.archived.append(kw["channel"])
        return {"ok": True}


class FakeApp:
    def __init__(self):
        self.client = FakeClient()


# ---- helpers -----------------------------------------------------------------

def fresh_handler():
    """Reset module-level state, return a (handle, app) for the test."""
    bridge.sessions.clear()
    app = FakeApp()
    return bridge.make_handler(app), app


def seed_session(channel="D1", cli="claude", path="/tmp/proj", **kw):
    sess = bridge.CLISession(
        cli=cli,
        tmux_name=kw.pop("tmux_name", f"slack_U_{channel}_{cli}"),
        path=path,
        slack_channel_id=channel if kw.get("is_named") else None,
        **kw,
    )
    bridge.sessions[channel] = sess
    return sess


def last_post_text(app, channel=None):
    for p in reversed(app.client.posts):
        if channel is None or p.get("channel") == channel:
            return p.get("text", "")
    return ""


# ---- !run --------------------------------------------------------------------

class RunCmdTests(unittest.TestCase):
    def setUp(self):
        self.handle, self.app = fresh_handler()

    def test_usage_when_empty(self):
        seed_session()
        self.handle("U", "D1", "!run", None)
        self.assertIn("Usage: `!run", last_post_text(self.app))

    def test_runs_in_session_cwd(self):
        seed_session(path="/tmp")
        with mock.patch.object(bridge.subprocess, "run") as runp:
            runp.return_value = subprocess.CompletedProcess(
                args="echo hi", returncode=0, stdout="hi\n", stderr="",
            )
            self.handle("U", "D1", "!run echo hi", None)
        runp.assert_called_once()
        kwargs = runp.call_args.kwargs
        self.assertEqual(runp.call_args.args[0], "echo hi")
        self.assertEqual(kwargs["cwd"], "/tmp")
        self.assertTrue(kwargs["shell"])
        self.assertIn("hi", last_post_text(self.app))

    def test_no_session_falls_back_to_cwd(self):
        with mock.patch.object(bridge.subprocess, "run") as runp:
            runp.return_value = subprocess.CompletedProcess(
                args="pwd", returncode=0, stdout="/somewhere\n", stderr="",
            )
            self.handle("U", "D1", "!run pwd", None)
        self.assertEqual(runp.call_args.kwargs["cwd"], os.getcwd())

    def test_timeout_message(self):
        seed_session()
        with mock.patch.object(bridge.subprocess, "run") as runp:
            runp.side_effect = subprocess.TimeoutExpired("sleep 60", 30)
            self.handle("U", "D1", "!run sleep 60", None)
        self.assertIn("timed out", last_post_text(self.app))

    def test_nonzero_exit_reported(self):
        seed_session()
        with mock.patch.object(bridge.subprocess, "run") as runp:
            runp.return_value = subprocess.CompletedProcess(
                args="false", returncode=2, stdout="", stderr="boom\n",
            )
            self.handle("U", "D1", "!run false", None)
        text = last_post_text(self.app)
        self.assertIn("boom", text)
        self.assertIn("exit 2", text)

    def test_stdout_truncation(self):
        seed_session()
        big = "x" * 10000
        with mock.patch.object(bridge.subprocess, "run") as runp:
            runp.return_value = subprocess.CompletedProcess(
                args="big", returncode=0, stdout=big, stderr="",
            )
            self.handle("U", "D1", "!run big", None)
        text = last_post_text(self.app)
        self.assertIn("truncated", text)
        # Posted text must not contain the full 10k blob.
        self.assertLess(len(text), 9000)


# ---- !download ---------------------------------------------------------------

class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.handle, self.app = fresh_handler()
        self.tmpdir = ROOT / "tests" / "_tmp_dl"
        self.tmpdir.mkdir(exist_ok=True)
        for p in self.tmpdir.iterdir():
            p.unlink()

    def tearDown(self):
        for p in self.tmpdir.iterdir():
            p.unlink()
        self.tmpdir.rmdir()

    def test_no_files_prompts_user(self):
        seed_session()
        self.handle("U", "D1", "!download", None, files=[])
        self.assertIn("Attach a file", last_post_text(self.app))

    def test_writes_file_into_session_cwd(self):
        seed_session(path=str(self.tmpdir))
        files = [{
            "url_private_download": "https://files.slack.com/abc",
            "name": "report.txt",
        }]

        class FakeResp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): self.close()

        with mock.patch.object(bridge.urllib.request, "urlopen") as op:
            op.return_value = FakeResp(b"hello world")
            self.handle("U", "D1", "!download", None, files=files)

        # Validate auth header on the request.
        req = op.call_args.args[0]
        self.assertEqual(req.headers["Authorization"],
                         f"Bearer {self.app.client.token}")

        dest = self.tmpdir / "report.txt"
        self.assertTrue(dest.exists())
        self.assertEqual(dest.read_bytes(), b"hello world")
        self.assertIn("Saved 1", last_post_text(self.app))

    def test_path_traversal_sanitized(self):
        seed_session(path=str(self.tmpdir))
        files = [{
            "url_private_download": "https://files.slack.com/abc",
            "name": "../../etc/passwd",
        }]

        class FakeResp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): self.close()

        with mock.patch.object(bridge.urllib.request, "urlopen") as op:
            op.return_value = FakeResp(b"x")
            self.handle("U", "D1", "!download", None, files=files)

        # File should land at <tmpdir>/passwd (basename only) — not outside it.
        self.assertTrue((self.tmpdir / "passwd").exists())
        self.assertFalse((Path("/etc") / "passwd_bridge_test").exists())  # sanity


# ---- !cancel -----------------------------------------------------------------

class CancelTests(unittest.TestCase):
    def setUp(self):
        self.handle, self.app = fresh_handler()

    def test_no_session_message(self):
        self.handle("U", "D1", "!cancel", None)
        self.assertIn("No active session", last_post_text(self.app))

    def test_sends_escape(self):
        sess = seed_session()
        with mock.patch.object(bridge, "_tmux") as tmuxp:
            self.handle("U", "D1", "!cancel", None)
        # Last call must be send-keys Escape to the right session.
        tmuxp.assert_called_with("send-keys", "-t", sess.tmux_name, "Escape")
        self.assertIn("interrupt", last_post_text(self.app))

    def test_aliases(self):
        seed_session()
        for alias in ("!interrupt", "!stop"):
            with mock.patch.object(bridge, "_tmux") as tmuxp:
                self.handle("U", "D1", alias, None)
            tmuxp.assert_called_with("send-keys", "-t", mock.ANY, "Escape")


# ---- !switch -----------------------------------------------------------------

class SwitchTests(unittest.TestCase):
    def setUp(self):
        self.handle, self.app = fresh_handler()

    def test_invalid_cli(self):
        seed_session(cli="claude")
        with mock.patch.object(bridge, "kill_session") as ks, \
             mock.patch.object(bridge, "start_session") as ss:
            self.handle("U", "D1", "!switch foo", None)
        ks.assert_not_called()
        ss.assert_not_called()
        self.assertIn("Usage: `!switch", last_post_text(self.app))

    def test_no_session(self):
        with mock.patch.object(bridge, "kill_session") as ks, \
             mock.patch.object(bridge, "start_session") as ss:
            self.handle("U", "D1", "!switch gemini", None)
        ks.assert_not_called()
        ss.assert_not_called()
        self.assertIn("No active session", last_post_text(self.app))

    def test_same_cli_noop(self):
        seed_session(cli="gemini")
        with mock.patch.object(bridge, "kill_session") as ks, \
             mock.patch.object(bridge, "start_session") as ss:
            self.handle("U", "D1", "!switch gemini", None)
        ks.assert_not_called()
        ss.assert_not_called()
        self.assertIn("Already running", last_post_text(self.app))

    def test_kills_and_relaunches(self):
        sess = seed_session(cli="claude", path="/tmp/p", extra_args="--model opus")
        with mock.patch.object(bridge, "kill_session") as ks, \
             mock.patch.object(bridge, "start_session", return_value=True) as ss:
            self.handle("U", "D1", "!switch gemini", None)
        ks.assert_called_once_with(sess.tmux_name)
        ss.assert_called_once()
        # New cli, same tmux name, same cwd; extra_args reset (different CLI).
        kwargs = ss.call_args.kwargs
        self.assertEqual(ss.call_args.args[0], "gemini")
        self.assertEqual(ss.call_args.args[1], sess.tmux_name)
        self.assertEqual(kwargs["cwd"], "/tmp/p")
        self.assertEqual(bridge.sessions["D1"].cli, "gemini")
        self.assertEqual(bridge.sessions["D1"].extra_args, "")
        self.assertIn("Switched to `gemini`", last_post_text(self.app))

    def test_relaunch_failure_drops_session(self):
        seed_session(cli="claude")
        with mock.patch.object(bridge, "kill_session"), \
             mock.patch.object(bridge, "start_session", return_value=False):
            self.handle("U", "D1", "!switch gemini", None)
        self.assertNotIn("D1", bridge.sessions)
        self.assertIn("Failed to start", last_post_text(self.app))


# ---- !model ------------------------------------------------------------------

class ModelTests(unittest.TestCase):
    def setUp(self):
        self.handle, self.app = fresh_handler()

    def test_no_session(self):
        self.handle("U", "D1", "!model opus", None)
        self.assertIn("No active session", last_post_text(self.app))

    def test_no_arg_shows_current(self):
        seed_session(cli="claude", extra_args="--model opus")
        self.handle("U", "D1", "!model", None)
        text = last_post_text(self.app)
        self.assertIn("--model opus", text)
        self.assertIn("Usage", text)

    def test_relaunch_with_claude_flag(self):
        sess = seed_session(cli="claude", path="/tmp/p")
        with mock.patch.object(bridge, "kill_session"), \
             mock.patch.object(bridge, "start_session", return_value=True) as ss:
            self.handle("U", "D1", "!model opus", None)
        self.assertEqual(ss.call_args.kwargs["extra_args"], "--model opus")
        self.assertEqual(ss.call_args.kwargs["cwd"], "/tmp/p")
        self.assertEqual(ss.call_args.args[0], "claude")
        self.assertEqual(ss.call_args.args[1], sess.tmux_name)
        self.assertEqual(bridge.sessions["D1"].extra_args, "--model opus")
        self.assertIn("Restarted", last_post_text(self.app))

    def test_relaunch_with_codex_flag(self):
        seed_session(cli="codex")
        with mock.patch.object(bridge, "kill_session"), \
             mock.patch.object(bridge, "start_session", return_value=True) as ss:
            self.handle("U", "D1", "!model gpt-5", None)
        self.assertEqual(ss.call_args.kwargs["extra_args"], "-m gpt-5")

    def test_relaunch_with_gemini_flag(self):
        seed_session(cli="gemini")
        with mock.patch.object(bridge, "kill_session"), \
             mock.patch.object(bridge, "start_session", return_value=True) as ss:
            self.handle("U", "D1", "!model gemini-2.5-flash", None)
        self.assertEqual(ss.call_args.kwargs["extra_args"], "-m gemini-2.5-flash")

    def test_unknown_cli_refuses(self):
        seed_session(cli="claude")
        with mock.patch.dict(bridge.CLI_CONFIGS["claude"], {}, clear=False):
            cfg = bridge.CLI_CONFIGS["claude"].copy()
            cfg.pop("model_flag", None)
            with mock.patch.dict(bridge.CLI_CONFIGS, {"claude": cfg}):
                self.handle("U", "D1", "!model opus", None)
        self.assertIn("does not support", last_post_text(self.app))

    def test_relaunch_failure_drops_session(self):
        seed_session(cli="claude")
        with mock.patch.object(bridge, "kill_session"), \
             mock.patch.object(bridge, "start_session", return_value=False):
            self.handle("U", "D1", "!model bogus-model-name", None)
        self.assertNotIn("D1", bridge.sessions)
        self.assertIn("Failed to relaunch", last_post_text(self.app))


# ---- !reset preserves extra_args --------------------------------------------

class ResetPreservesModelTests(unittest.TestCase):
    def setUp(self):
        self.handle, self.app = fresh_handler()

    def test_reset_passes_extra_args(self):
        sess = seed_session(cli="claude", path="/tmp/p", extra_args="--model opus")
        with mock.patch.object(bridge, "kill_session"), \
             mock.patch.object(bridge, "start_session", return_value=True) as ss:
            self.handle("U", "D1", "!reset", None)
        self.assertEqual(ss.call_args.kwargs["extra_args"], "--model opus")
        self.assertEqual(ss.call_args.kwargs["cwd"], "/tmp/p")
        self.assertEqual(ss.call_args.args[0], "claude")
        self.assertEqual(ss.call_args.args[1], sess.tmux_name)
        self.assertIn("Restarted", last_post_text(self.app))

    def test_reset_no_session(self):
        self.handle("U", "D1", "!reset", None)
        self.assertIn("No active session", last_post_text(self.app))

    def test_reset_failure_drops_session(self):
        seed_session(cli="claude")
        with mock.patch.object(bridge, "kill_session"), \
             mock.patch.object(bridge, "start_session", return_value=False):
            self.handle("U", "D1", "!reset", None)
        self.assertNotIn("D1", bridge.sessions)


# ---- !sessions ---------------------------------------------------------------

class SessionsListTests(unittest.TestCase):
    def setUp(self):
        self.handle, self.app = fresh_handler()

    def test_empty(self):
        self.handle("U", "D1", "!sessions", None)
        self.assertIn("No active sessions", last_post_text(self.app))

    def test_lists_all(self):
        seed_session(channel="D1", cli="claude", path="/tmp/p1")
        seed_session(channel="C9", cli="codex", path="/tmp/p2",
                     is_named=True, tmux_name="codex-foo-b93c")
        self.handle("U", "D1", "!sessions", None)
        text = last_post_text(self.app)
        self.assertIn("2 active session", text)
        self.assertIn("`claude`", text)
        self.assertIn("`codex`", text)
        self.assertIn("/tmp/p1", text)
        self.assertIn("/tmp/p2", text)
        self.assertIn("<#C9>", text)  # named → channel link
        self.assertIn("`D1`", text)   # DM → raw id

    def test_aliases(self):
        seed_session()
        for alias in ("!ls", "!list"):
            self.app.client.posts.clear()
            self.handle("U", "D1", alias, None)
            self.assertIn("active session", last_post_text(self.app))


# ---- idle reaper disabled ---------------------------------------------------

class IdleDisabledTests(unittest.TestCase):
    def test_idle_constant_is_none(self):
        self.assertIsNone(bridge.IDLE_TIMEOUT_SEC)

    def test_cleanup_loop_is_noop_when_idle_none(self):
        # The loop runs forever; we drive it manually for a bounded number of
        # iterations by patching time.sleep to raise after one call.
        seed_session()
        before = dict(bridge.sessions)

        class StopAfterFirstSleep(Exception): pass

        def fake_sleep(_): raise StopAfterFirstSleep
        with mock.patch.object(bridge.time, "sleep", side_effect=fake_sleep):
            with self.assertRaises(StopAfterFirstSleep):
                bridge.cleanup_idle_loop(app=None)

        # No session should have been reaped.
        self.assertEqual(set(before), set(bridge.sessions))


# ---- existing pure functions: regression ------------------------------------

class CleanOutputRegressionTests(unittest.TestCase):
    def test_strips_ansi_and_box_drawing(self):
        raw = "\x1b[31mhello\x1b[0m\n│  ╭───╮\n● answer line\n"
        out = bridge.clean_output(raw)
        self.assertIn("hello", out)
        self.assertIn("answer line", out)
        self.assertNotIn("\x1b[", out)
        self.assertNotIn("╭", out)
        # Leading "● " marker stripped.
        self.assertFalse(out.lstrip().startswith("●"))

    def test_drops_press_esc_to_interrupt(self):
        raw = "answer\nPress Esc to interrupt and edit your previous message"
        self.assertNotIn("Esc", bridge.clean_output(raw))


if __name__ == "__main__":
    unittest.main()
