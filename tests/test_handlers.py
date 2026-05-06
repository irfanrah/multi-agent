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
import time
import unittest
import zipfile
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
        ts = str(len(self.posts) + 1)
        record = dict(kw, ts=ts)
        self.posts.append(record)
        return {"ts": ts, "channel": kw.get("channel")}

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


def latest_visible_text(app, channel=None):
    """Most recent text the user would see — accounts for chat.update
    overwriting an earlier placeholder. Walks both posts and updates by ts."""
    rows = []
    for p in app.client.posts:
        if channel is None or p.get("channel") == channel:
            rows.append((p.get("ts"), p.get("text", "")))
    # Apply updates: latest update for a given ts wins.
    by_ts = dict(rows)
    for u in app.client.updates:
        if channel is None or u.get("channel") == channel:
            if u.get("ts") in by_ts:
                by_ts[u["ts"]] = u.get("text", "")
    if not by_ts:
        return ""
    # Return the text for the most-recently-touched ts.
    last_ts = None
    for ts, _ in rows:
        last_ts = ts
    for u in app.client.updates:
        if channel is None or u.get("channel") == channel:
            last_ts = u.get("ts", last_ts)
    return by_ts.get(last_ts, "")


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


# ---- !upload folder-zip ------------------------------------------------------

class UploadFolderTests(unittest.TestCase):
    def setUp(self):
        self.handle, self.app = fresh_handler()
        # Build a tree under a tempdir that we can pass to !upload absolutely.
        import tempfile as _t
        self.td = _t.mkdtemp(prefix="bridge_uptest_")
        # Real files we expect to ship.
        os.makedirs(os.path.join(self.td, "src"))
        Path(self.td, "src", "main.py").write_text("print('hi')\n")
        Path(self.td, "README.md").write_text("readme\n")
        # Junk we expect to skip.
        os.makedirs(os.path.join(self.td, ".git"))
        Path(self.td, ".git", "HEAD").write_text("ref: refs/heads/main\n")
        os.makedirs(os.path.join(self.td, "__pycache__"))
        Path(self.td, "__pycache__", "x.pyc").write_text("\x00\x00\x00\x00")
        os.makedirs(os.path.join(self.td, "node_modules"))
        Path(self.td, "node_modules", "lib.js").write_text("module.exports={}\n")

    def tearDown(self):
        import shutil as _s
        _s.rmtree(self.td, ignore_errors=True)

    def _zip_path_from_call(self, call):
        # files_upload_v2 receives the zip path via the `file` kwarg. We need
        # to peek at the file BEFORE the handler unlinks it; the FakeClient
        # doesn't read the bytes, so the zip is still on disk inside its call,
        # but is removed in the handler's `finally`. Workaround: copy the
        # file aside on first call.
        return call["file"]

    def test_directory_zips_and_uploads(self):
        seed_session()
        # Capture the zip's contents at call time before the handler deletes it.
        captured_namelist = {}

        def fake_upload(**kw):
            with zipfile.ZipFile(kw["file"]) as zf:
                captured_namelist["names"] = sorted(zf.namelist())
                captured_namelist["title"] = kw.get("title")
            self.app.client.uploads.append(kw)
            return {"ok": True}

        with mock.patch.object(self.app.client, "files_upload_v2", side_effect=fake_upload):
            # --direct skips the picker.
            self.handle("U", "D1", f"!upload --direct {self.td}", None)

        self.assertEqual(len(self.app.client.uploads), 1)
        names = captured_namelist["names"]
        base = os.path.basename(self.td)
        self.assertIn(f"{base}/src/main.py", names)
        self.assertIn(f"{base}/README.md", names)
        # Junk excluded.
        self.assertFalse(any(".git/" in n for n in names),
                         f".git/ should be excluded, got: {names}")
        self.assertFalse(any("__pycache__/" in n for n in names))
        self.assertFalse(any("node_modules/" in n for n in names))
        self.assertEqual(captured_namelist["title"], f"{base}.zip")
        self.assertIn("Uploaded 1", last_post_text(self.app))

    def test_directory_size_cap_refuses(self):
        seed_session()
        # Cap is 1 GB; lie about size to trip it.
        with mock.patch.object(bridge, "directory_size_for_zip",
                               return_value=2 * 1024 * 1024 * 1024):
            with mock.patch.object(self.app.client, "files_upload_v2") as up:
                self.handle("U", "D1", f"!upload --direct {self.td}", None)
        up.assert_not_called()
        text = last_post_text(self.app)
        self.assertIn("MB", text)
        self.assertIn("cap", text)

    def test_mixed_file_and_directory(self):
        seed_session()
        single_file = os.path.join(self.td, "README.md")
        captured = []

        def fake_upload(**kw):
            captured.append(kw.get("title"))
            return {"ok": True}

        with mock.patch.object(self.app.client, "files_upload_v2", side_effect=fake_upload):
            # Multi-path always goes direct; no picker.
            self.handle("U", "D1", f"!upload {single_file} {self.td}", None)
        # Both should have been uploaded — the file plus the dir-as-zip.
        self.assertIn("README.md", captured)
        self.assertIn(f"{os.path.basename(self.td)}.zip", captured)
        self.assertIn("Uploaded 2", last_post_text(self.app))

    def test_empty_directory_after_exclusions(self):
        # Wipe the real-content dirs, leave only `.git`.
        for sub in ("src", "README.md", "__pycache__", "node_modules"):
            p = Path(self.td, sub)
            if p.is_dir():
                import shutil as _s
                _s.rmtree(p)
            elif p.exists():
                p.unlink()
        seed_session()
        with mock.patch.object(self.app.client, "files_upload_v2") as up:
            self.handle("U", "D1", f"!upload --direct {self.td}", None)
        up.assert_not_called()
        self.assertIn("no files to zip", last_post_text(self.app))

    def test_zip_directory_helper_excludes_junk(self):
        # Pure helper-level test: zip_directory must exclude the same set the
        # handler reports, regardless of where it's called from.
        out = os.path.join(self.td, "out.zip")
        try:
            n = bridge.zip_directory(self.td, out)
            with zipfile.ZipFile(out) as zf:
                names = zf.namelist()
            base = os.path.basename(self.td)
            self.assertGreater(n, 0)
            self.assertIn(f"{base}/src/main.py", names)
            self.assertIn(f"{base}/README.md", names)
            for junk in (".git/HEAD", "__pycache__/x.pyc", "node_modules/lib.js"):
                self.assertFalse(any(n.endswith(junk) for n in names),
                                 f"junk leaked into zip: {junk} in {names}")
        finally:
            if os.path.exists(out):
                os.unlink(out)


# ---- !upload menu (1/2 picker) + pixeldrain link path -----------------------

class UploadMenuTests(unittest.TestCase):
    def setUp(self):
        self.handle, self.app = fresh_handler()
        # Reset the module-level pending dict so tests don't leak state.
        with bridge.pending_uploads_lock:
            bridge.pending_uploads.clear()
        import tempfile as _t
        self.td = _t.mkdtemp(prefix="bridge_menutest_")
        os.makedirs(os.path.join(self.td, "src"))
        Path(self.td, "src", "main.py").write_text("print('hi')\n")
        Path(self.td, "README.md").write_text("readme\n")

    def tearDown(self):
        import shutil as _s
        _s.rmtree(self.td, ignore_errors=True)
        with bridge.pending_uploads_lock:
            bridge.pending_uploads.clear()

    # -- picker prompt --------------------------------------------------------

    def test_single_path_prompts_menu(self):
        seed_session()
        with mock.patch.object(self.app.client, "files_upload_v2") as up:
            self.handle("U", "D1", f"!upload {self.td}", None)
        up.assert_not_called()
        text = last_post_text(self.app)
        self.assertIn("Upload", text)
        self.assertIn("`1`", text)
        self.assertIn("`2`", text)
        self.assertIn("pixeldrain", text)
        # Pending state recorded for this channel.
        with bridge.pending_uploads_lock:
            self.assertIn("D1", bridge.pending_uploads)
            self.assertEqual(bridge.pending_uploads["D1"]["source"], self.td)

    def test_pick_1_runs_direct_upload(self):
        seed_session()
        # First message stages the pending upload + posts the menu.
        with mock.patch.object(self.app.client, "files_upload_v2") as up_a:
            self.handle("U", "D1", f"!upload {self.td}", None)
        up_a.assert_not_called()

        # Pick "1" — should drain pending and run the direct flow.
        captured = []
        def fake_upload(**kw):
            captured.append(kw.get("title"))
            return {"ok": True}
        with mock.patch.object(self.app.client, "files_upload_v2", side_effect=fake_upload):
            self.handle("U", "D1", "1", None)
        self.assertIn(f"{os.path.basename(self.td)}.zip", captured)
        self.assertIn("Uploaded 1", last_post_text(self.app))
        with bridge.pending_uploads_lock:
            self.assertNotIn("D1", bridge.pending_uploads)

    def test_pick_2_runs_link_upload(self):
        seed_session()
        self.handle("U", "D1", f"!upload {self.td}", None)
        # Inspect the zip *inside* the mocked PUT call — the handler's `finally`
        # deletes the temp dir on return, so we can't inspect after the fact.
        captured = {}

        def fake_put(zip_path, **_kw):
            captured["path"] = zip_path
            captured["exists"] = os.path.isfile(zip_path)
            with zipfile.ZipFile(zip_path) as zf:
                captured["names"] = sorted(zf.namelist())
                zf.setpassword(bridge.LINK_UPLOAD_PASSWORD.encode())
                base = os.path.basename(self.td)
                captured["payload"] = zf.read(f"{base}/src/main.py")
                # Reading without a password must fail (proof it's encrypted).
                zf.setpassword(None)
                try:
                    zf.read(f"{base}/src/main.py")
                    captured["unencrypted"] = True
                except RuntimeError:
                    captured["unencrypted"] = False
            return "https://pixeldrain.com/u/xyz"

        with mock.patch.object(bridge, "pixeldrain_put", side_effect=fake_put), \
             mock.patch.object(self.app.client, "files_upload_v2") as up:
            self.handle("U", "D1", "2", None)
        up.assert_not_called()  # Slack file-upload not used in link flow.
        self.assertTrue(captured["exists"])
        base = os.path.basename(self.td)
        self.assertTrue(any(n.startswith(f"{base}/") for n in captured["names"]))
        self.assertIn(b"print", captured["payload"])
        self.assertFalse(captured["unencrypted"],
                         "zip must require the password to read")
        text = latest_visible_text(self.app)
        self.assertIn("pixeldrain.com/u/xyz", text)
        self.assertIn(bridge.LINK_UPLOAD_PASSWORD, text)
        with bridge.pending_uploads_lock:
            self.assertNotIn("D1", bridge.pending_uploads)

    def test_bang_prefixed_pick_works(self):
        seed_session()
        self.handle("U", "D1", f"!upload {self.td}", None)
        with mock.patch.object(self.app.client, "files_upload_v2") as up:
            self.handle("U", "D1", "!1", None)
        up.assert_called_once()

    def test_pick_with_no_pending_falls_through(self):
        # No pending — bare "1" must NOT consume the menu and must NOT post a
        # spurious message. (It falls through to the "no active session"
        # message below since there's no CLI session in this test fixture.)
        with mock.patch.object(self.app.client, "files_upload_v2") as up:
            self.handle("U", "D1", "1", None)
        up.assert_not_called()
        # Whatever else gets posted, it must not look like an upload report.
        for p in self.app.client.posts:
            self.assertNotIn("Uploaded", p.get("text", ""))
            self.assertNotIn("pixeldrain", p.get("text", ""))

    def test_pick_after_ttl_expired_falls_through(self):
        seed_session()
        self.handle("U", "D1", f"!upload {self.td}", None)
        # Force-expire the pending entry.
        with bridge.pending_uploads_lock:
            bridge.pending_uploads["D1"]["expires_at"] = time.time() - 1
        with mock.patch.object(self.app.client, "files_upload_v2") as up:
            self.handle("U", "D1", "1", None)
        up.assert_not_called()

    # -- explicit flag forms (no picker) -------------------------------------

    def test_link_flag_skips_menu(self):
        seed_session()
        with mock.patch.object(bridge, "pixeldrain_put",
                               return_value="https://pixeldrain.com/u/yzw") as tput:
            self.handle("U", "D1", f"!upload --link {self.td}", None)
        tput.assert_called_once()
        # No pending stored (we went straight to the link flow).
        with bridge.pending_uploads_lock:
            self.assertNotIn("D1", bridge.pending_uploads)

    def test_direct_flag_skips_menu(self):
        seed_session()
        with mock.patch.object(self.app.client, "files_upload_v2") as up:
            self.handle("U", "D1", f"!upload --direct {self.td}", None)
        up.assert_called_once()


class PixeldrainPutTests(unittest.TestCase):
    """pixeldrain_put PUTs the file body to the right URL, parses the JSON
    response, and returns the `https://<host>/u/<id>` viewer link."""

    def test_put_method_url_and_returns_link(self):
        import tempfile as _t
        with _t.NamedTemporaryFile(suffix=".zip", delete=False) as f:
            f.write(b"\x50\x4b\x03\x04test-zip-bytes")
            path = f.name
        try:
            class FakeResp:
                def __init__(self, body): self._body = body
                def read(self): return self._body
                def __enter__(self): return self
                def __exit__(self, *a): return False
            with mock.patch.object(bridge.urllib.request, "urlopen") as op:
                op.return_value = FakeResp(b'{"id":"abc123","success":true}')
                link = bridge.pixeldrain_put(path,
                                             endpoint="https://pixeldrain.com")
            self.assertEqual(link, "https://pixeldrain.com/u/abc123")
            # Inspect the Request object passed to urlopen.
            req = op.call_args.args[0]
            self.assertEqual(req.method, "PUT")
            self.assertTrue(req.full_url.startswith("https://pixeldrain.com/api/file/"))
            self.assertTrue(req.full_url.endswith(os.path.basename(path)))
            self.assertEqual(req.data[:4], b"\x50\x4b\x03\x04")
        finally:
            os.unlink(path)

    def test_non_json_response_raises(self):
        import tempfile as _t
        with _t.NamedTemporaryFile(suffix=".zip", delete=False) as f:
            f.write(b"x")
            path = f.name
        try:
            class FakeResp:
                def __init__(self, body): self._body = body
                def read(self): return self._body
                def __enter__(self): return self
                def __exit__(self, *a): return False
            with mock.patch.object(bridge.urllib.request, "urlopen") as op:
                op.return_value = FakeResp(b"<html>503 Service Unavailable</html>")
                with self.assertRaises(RuntimeError):
                    bridge.pixeldrain_put(path)
        finally:
            os.unlink(path)

    def test_json_without_id_raises(self):
        import tempfile as _t
        with _t.NamedTemporaryFile(suffix=".zip", delete=False) as f:
            f.write(b"x")
            path = f.name
        try:
            class FakeResp:
                def __init__(self, body): self._body = body
                def read(self): return self._body
                def __enter__(self): return self
                def __exit__(self, *a): return False
            with mock.patch.object(bridge.urllib.request, "urlopen") as op:
                op.return_value = FakeResp(b'{"success":false,"message":"bad"}')
                with self.assertRaises(RuntimeError):
                    bridge.pixeldrain_put(path)
        finally:
            os.unlink(path)


class EncryptedZipBuilderTests(unittest.TestCase):
    """make_encrypted_zip_for_upload produces password-protected archives
    decryptable with the bridge's LINK_UPLOAD_PASSWORD."""

    def setUp(self):
        import tempfile as _t
        self.td = _t.mkdtemp(prefix="bridge_enczip_")

    def tearDown(self):
        import shutil as _s
        _s.rmtree(self.td, ignore_errors=True)

    def test_encrypts_single_file(self):
        src = os.path.join(self.td, "report.txt")
        Path(src).write_text("secret payload\n")
        out = os.path.join(self.td, "out.zip")
        n = bridge.make_encrypted_zip_for_upload(
            src, out, bridge.LINK_UPLOAD_PASSWORD)
        self.assertEqual(n, 1)
        with zipfile.ZipFile(out) as zf:
            self.assertEqual(zf.namelist(), ["report.txt"])
            zf.setpassword(bridge.LINK_UPLOAD_PASSWORD.encode())
            self.assertEqual(zf.read("report.txt"), b"secret payload\n")

    def test_encrypts_directory_excluding_junk(self):
        src = os.path.join(self.td, "proj")
        os.makedirs(os.path.join(src, "src"))
        Path(src, "src", "a.py").write_text("x = 1\n")
        os.makedirs(os.path.join(src, ".git"))
        Path(src, ".git", "HEAD").write_text("ref\n")
        out = os.path.join(self.td, "proj.zip")
        n = bridge.make_encrypted_zip_for_upload(
            src, out, bridge.LINK_UPLOAD_PASSWORD)
        self.assertEqual(n, 1)  # only src/a.py; .git skipped
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
            self.assertIn("proj/src/a.py", names)
            self.assertFalse(any(".git" in nm for nm in names))
            zf.setpassword(bridge.LINK_UPLOAD_PASSWORD.encode())
            self.assertEqual(zf.read("proj/src/a.py"), b"x = 1\n")


# ---- three-tier fallback helpers --------------------------------------------

class PendingDialogTests(unittest.TestCase):
    """extract_pending_dialog scans the pane for a permission prompt and
    returns the question + options, clipped at the dialog footer."""

    def test_returns_empty_when_no_dialog(self):
        with mock.patch.object(bridge, "capture", return_value="some output\nnothing here"):
            self.assertEqual(bridge.extract_pending_dialog("any"), "")

    def test_finds_apply_this_change(self):
        pane = (
            "  some old work\n"
            "\n"
            "  Apply this change?\n"
            "\n"
            "  ● 1. Allow once\n"
            "    2. Allow for this session\n"
            "    3. Modify with external editor\n"
            "    4. No, suggest changes (esc)\n"
            "\n"
            " > Type your message or @path/to/file\n"
        )
        with mock.patch.object(bridge, "capture", return_value=pane):
            block = bridge.extract_pending_dialog("any")
        self.assertIn("Apply this change?", block)
        self.assertIn("1. Allow once", block)
        self.assertIn("4. No, suggest changes", block)

    def test_finds_allow_execution(self):
        pane = (
            "  Allow execution of [python3]?\n"
            "\n"
            "  ● 1. Allow once\n"
            "    2. Allow for this session\n"
            "    3. No, suggest changes (esc)\n"
        )
        with mock.patch.object(bridge, "capture", return_value=pane):
            block = bridge.extract_pending_dialog("any")
        self.assertIn("Allow execution", block)
        self.assertIn("1. Allow once", block)

    def test_returns_most_recent_when_multiple(self):
        # An earlier (already-answered) dialog plus a fresh one.
        pane = (
            "  Allow execution of [chmod]?\n"
            "  ● 1. Allow once\n"
            "    2. Allow for this session\n"
            "  Done\n"
            "\n"
            "  Apply this change?\n"
            "  ● 1. Allow once\n"
            "    2. Allow for this session\n"
        )
        with mock.patch.object(bridge, "capture", return_value=pane):
            block = bridge.extract_pending_dialog("any")
        self.assertIn("Apply this change?", block)
        self.assertNotIn("[chmod]", block)


class PaneTailAfterUserInputTests(unittest.TestCase):
    """pane_tail_after_user_input slices from the user's last echoed input."""

    def test_returns_empty_when_input_not_in_pane(self):
        with mock.patch.object(bridge, "capture", return_value="other content"):
            self.assertEqual(
                bridge.pane_tail_after_user_input("any", "hello"), "")

    def test_returns_only_content_after_user_echo(self):
        pane = (
            "OLD STALE RESPONSE FROM PREVIOUS TURN\n"
            "more old content\n"
            " > make a new script\n"
            "  Working on it...\n"
            "  Generated script.sh\n"
            "  Done\n"
        )
        with mock.patch.object(bridge, "capture", return_value=pane):
            out = bridge.pane_tail_after_user_input(
                "any", "make a new script",
                max_lines=20)
        # Old content above the user echo is excluded.
        self.assertNotIn("OLD STALE RESPONSE", out)
        self.assertNotIn("more old content", out)
        # New content below is included.
        self.assertIn("Working on it", out)
        self.assertIn("Generated script.sh", out)

    def test_caps_at_max_lines(self):
        # 50 fresh lines after the user echo, max_lines=10 → only last 10.
        pane = " > my prompt\n" + "\n".join(f"line {i}" for i in range(50)) + "\n"
        with mock.patch.object(bridge, "capture", return_value=pane):
            out = bridge.pane_tail_after_user_input("any", "my prompt", max_lines=10)
        self.assertEqual(len(out.split("\n")), 10)
        self.assertIn("line 49", out)
        self.assertNotIn("line 39", out)

    def test_strips_via_clean_output(self):
        pane = " > the prompt\n\x1b[31mcolor\x1b[0m\n│  ╭───╮\nplain text\n"
        with mock.patch.object(bridge, "capture", return_value=pane):
            out = bridge.pane_tail_after_user_input("any", "the prompt")
        self.assertNotIn("\x1b[", out)
        self.assertNotIn("╭", out)
        self.assertIn("color", out)
        self.assertIn("plain text", out)

    def test_strips_gemini_tui_chrome(self):
        # Regression: when gemini is in shell mode and produces no real
        # response, we used to dump the whole TUI frame ("? for shortcuts",
        # ▄▄▄, "! Type your shell command", workspace footer, etc) at the
        # user. Those should all be stripped.
        pane = (
            " > my prompt\n"
            "                                                          ? for shortcuts\n"
            "  shell mode enabled (esc to disable)\n"
            "▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄\n"
            " !   Type your shell command\n"
            " workspace (/directory)        branch    sandbox    /model    quota\n"
            " /home/kurnianto/code/CCTV     main      no sandbox Auto (Gemini 3)  2% used\n"
            "real progress line we want to see\n"
        )
        with mock.patch.object(bridge, "capture", return_value=pane):
            out = bridge.pane_tail_after_user_input("any", "my prompt", max_lines=20)
        self.assertNotIn("? for shortcuts", out)
        self.assertNotIn("shell mode enabled", out)
        self.assertNotIn("Type your shell command", out)
        self.assertNotIn("workspace (/directory)", out)
        self.assertNotIn("Auto (Gemini", out)
        # ▄ should be eaten by BOX_RE inside clean_output.
        self.assertNotIn("▄", out)
        self.assertIn("real progress line", out)


# ---- dispatcher: three-tier fallback flow -----------------------------------

class DispatcherFallbackTests(unittest.TestCase):
    """When send_and_wait returns "", the dispatcher should:
       1. Surface a pending permission dialog if there is one.
       2. Otherwise show user-anchored recent activity if there is any.
       3. Otherwise post a "still working" message — never the bottom of the
          pane (which can be stale)."""

    def setUp(self):
        self.handle, self.app = fresh_handler()

    def _last_update(self):
        return self.app.client.updates[-1].get("text", "") if self.app.client.updates else ""

    def test_priority_1_permission_dialog(self):
        seed_session()
        with mock.patch.object(bridge, "send_and_wait", return_value=""), \
             mock.patch.object(bridge, "extract_pending_dialog",
                               return_value="Apply this change?\n  ● 1. Allow once"), \
             mock.patch.object(bridge, "pane_tail_after_user_input") as anchored:
            self.handle("U", "D1", "yes do it", None)
        # Dialog wins; anchored fallback never runs.
        anchored.assert_not_called()
        text = self._last_update()
        self.assertIn("PERMISSION DIALOG", text)
        self.assertIn("Apply this change?", text)
        self.assertIn("1. Allow once", text)

    def test_priority_2_anchored_recent_activity(self):
        seed_session()
        with mock.patch.object(bridge, "send_and_wait", return_value=""), \
             mock.patch.object(bridge, "extract_pending_dialog", return_value=""), \
             mock.patch.object(bridge, "pane_tail_after_user_input",
                               return_value="line a\nline b") as anchored:
            self.handle("U", "D1", "what is the progress", None)
        anchored.assert_called_once()
        text = self._last_update()
        self.assertIn("AGENT STILL RENDERING", text)
        self.assertIn("line a", text)
        # No "extracted reply was empty" preamble (old behavior).
        self.assertNotIn("extracted reply was empty", text)

    def test_priority_3_still_working(self):
        seed_session()
        with mock.patch.object(bridge, "send_and_wait", return_value=""), \
             mock.patch.object(bridge, "extract_pending_dialog", return_value=""), \
             mock.patch.object(bridge, "pane_tail_after_user_input", return_value=""):
            self.handle("U", "D1", "anything", None)
        text = self._last_update()
        self.assertIn("busy", text)
        self.assertIn("!cancel", text)
        self.assertIn("!raw", text)

    def test_normal_response_skips_all_fallbacks(self):
        seed_session()
        with mock.patch.object(bridge, "send_and_wait",
                               return_value="actual reply"), \
             mock.patch.object(bridge, "extract_pending_dialog") as ed, \
             mock.patch.object(bridge, "pane_tail_after_user_input") as anchored:
            self.handle("U", "D1", "hello", None)
        ed.assert_not_called()
        anchored.assert_not_called()


# ---- empty-reply fallback + !raw -------------------------------------------

# EmptyReplyFallbackTests removed — superseded by DispatcherFallbackTests
# above, which exercises the new three-tier fallback (dialog → user-anchored
# tail → still-working). The old single-tier `pane_tail` fallback was the
# source of the "stale captions table" bug we set out to fix.


class RawCommandTests(unittest.TestCase):
    def setUp(self):
        self.handle, self.app = fresh_handler()

    def test_no_session(self):
        self.handle("U", "D1", "!raw", None)
        self.assertIn("No active session", last_post_text(self.app))

    def test_default_dumps_pane_tail(self):
        seed_session()
        sample = "\n".join(f"line {i}" for i in range(10))
        with mock.patch.object(bridge, "pane_tail", return_value=sample) as pt:
            self.handle("U", "D1", "!raw", None)
        pt.assert_called_once()
        # Default n=60.
        self.assertEqual(pt.call_args.kwargs.get("n"), 60)
        text = last_post_text(self.app)
        self.assertIn("line 0", text)
        self.assertIn("line 9", text)

    def test_custom_n(self):
        seed_session()
        with mock.patch.object(bridge, "pane_tail", return_value="ok") as pt:
            self.handle("U", "D1", "!raw 5", None)
        self.assertEqual(pt.call_args.kwargs.get("n"), 5)

    def test_n_is_clamped(self):
        seed_session()
        with mock.patch.object(bridge, "pane_tail", return_value="ok") as pt:
            self.handle("U", "D1", "!raw 99999", None)
        self.assertEqual(pt.call_args.kwargs.get("n"), 500)
        with mock.patch.object(bridge, "pane_tail", return_value="ok") as pt:
            self.handle("U", "D1", "!raw 0", None)
        self.assertEqual(pt.call_args.kwargs.get("n"), 1)

    def test_invalid_n_shows_usage(self):
        seed_session()
        with mock.patch.object(bridge, "pane_tail") as pt:
            self.handle("U", "D1", "!raw foo", None)
        pt.assert_not_called()
        self.assertIn("Usage:", last_post_text(self.app))

    def test_empty_pane_message(self):
        seed_session()
        with mock.patch.object(bridge, "pane_tail", return_value=""):
            self.handle("U", "D1", "!raw", None)
        self.assertIn("empty after cleanup", last_post_text(self.app))

    def test_aliases(self):
        seed_session()
        with mock.patch.object(bridge, "pane_tail", return_value="ok"):
            for alias in ("!pane", "!tail"):
                self.app.client.posts.clear()
                self.handle("U", "D1", alias, None)
                self.assertIn("ok", last_post_text(self.app))


class PaneTailHelperTests(unittest.TestCase):
    """Helper-level: pane_tail uses capture+clean_output and obeys n."""

    def test_returns_last_n_lines(self):
        body = "\n".join(f"line{i}" for i in range(100))
        with mock.patch.object(bridge, "capture", return_value=body):
            out = bridge.pane_tail("any-session", n=5)
        self.assertEqual(out.split("\n"), ["line95", "line96", "line97", "line98", "line99"])

    def test_returns_all_when_short(self):
        body = "a\nb\nc"
        with mock.patch.object(bridge, "capture", return_value=body):
            out = bridge.pane_tail("any-session", n=10)
        self.assertEqual(out, "a\nb\nc")

    def test_strips_ansi_via_clean_output(self):
        body = "\x1b[31mhello\x1b[0m\n│ ─── \nworld"
        with mock.patch.object(bridge, "capture", return_value=body):
            out = bridge.pane_tail("any-session", n=10)
        self.assertNotIn("\x1b[", out)
        self.assertIn("hello", out)
        self.assertIn("world", out)


# ---- unknown !command must NOT be forwarded to the CLI ---------------------

class UnknownCommandTests(unittest.TestCase):
    """Typos like `!session` (singular) must not get typed into the CLI as
    raw text. Forwarding them flips gemini into shell-mode and bricks the
    conversation. The bridge should reject and suggest `!help`."""

    def setUp(self):
        self.handle, self.app = fresh_handler()

    def test_typo_is_not_forwarded(self):
        seed_session()
        # send_and_wait must NEVER be called for unknown !commands.
        with mock.patch.object(bridge, "send_and_wait") as saw:
            self.handle("U", "D1", "!session", None)  # singular typo
        saw.assert_not_called()
        text = last_post_text(self.app)
        self.assertIn("Unknown bridge command", text)
        self.assertIn("`!session`", text)
        self.assertIn("!help", text)

    def test_only_leading_bang_is_intercepted(self):
        # A message with `!` mid-text (not as the first char) IS forwarded —
        # users may legitimately want to send "fix the !important flag" to
        # the CLI without bridge interception.
        seed_session()
        with mock.patch.object(bridge, "send_and_wait", return_value="ok") as saw:
            self.handle("U", "D1", "fix the !important flag", None)
        saw.assert_called_once()

    def test_known_commands_still_work(self):
        # Sanity: !sessions, !help, etc. still match their handlers and DON'T
        # hit the unknown-command refusal.
        seed_session()
        self.handle("U", "D1", "!sessions", None)
        text = last_post_text(self.app)
        self.assertNotIn("Unknown bridge command", text)


# ---- auto-relink: recover named sessions across bridge restarts -------------

class AutoRelinkTests(unittest.TestCase):
    """When a free-text message arrives in a channel with no in-memory
    session, the bridge should look up the channel name and try to
    re-attach to a still-live tmux session of the same name. Recovers
    from bridge restarts / SSL races where the in-memory `sessions`
    dict was wiped but the tmux pane is still alive."""

    def setUp(self):
        self.handle, self.app = fresh_handler()

    def _set_channel_name(self, name):
        # Make app.client.conversations_info return the given channel name.
        self.app.client.conversations_info = lambda **kw: {
            "channel": {"id": kw["channel"], "name": name}
        }

    def test_relink_named_session_when_tmux_alive(self):
        self._set_channel_name("gemini-foo-1234")
        # No session in dict, but tmux session with the right name exists.
        with mock.patch.object(bridge, "session_exists", return_value=True), \
             mock.patch.object(bridge, "send_and_wait", return_value="hi from agent"):
            self.handle("U", "C123", "free-text message", None)
        # Session should now be in the dict.
        with bridge.sessions_lock:
            sess = bridge.sessions.get("C123")
        self.assertIsNotNone(sess, "auto-relink should have created a CLISession")
        self.assertEqual(sess.cli, "gemini")
        self.assertEqual(sess.tmux_name, "gemini-foo-1234")
        self.assertTrue(sess.is_named)

    def test_relink_skips_unrecognized_pattern(self):
        self._set_channel_name("random-channel")
        with mock.patch.object(bridge, "session_exists", return_value=True):
            self.handle("U", "C123", "free-text message", None)
        with bridge.sessions_lock:
            self.assertNotIn("C123", bridge.sessions)
        self.assertIn("No active session", last_post_text(self.app))

    def test_relink_skips_when_tmux_gone(self):
        self._set_channel_name("gemini-foo-1234")
        # Channel name matches but tmux session doesn't exist anymore.
        with mock.patch.object(bridge, "session_exists", return_value=False):
            self.handle("U", "C123", "free-text", None)
        with bridge.sessions_lock:
            self.assertNotIn("C123", bridge.sessions)
        self.assertIn("No active session", last_post_text(self.app))

    def test_relink_handles_conversations_info_error(self):
        # Slack call fails for some reason — no recovery, fallthrough to
        # the existing "no session" message.
        def boom(**kw):
            raise RuntimeError("conversations.info exploded")
        self.app.client.conversations_info = boom
        with mock.patch.object(bridge, "session_exists", return_value=True):
            self.handle("U", "C123", "free-text", None)
        with bridge.sessions_lock:
            self.assertNotIn("C123", bridge.sessions)
        self.assertIn("No active session", last_post_text(self.app))

    def test_existing_session_skips_relink(self):
        # If sessions[channel] is already set, relink shouldn't even be tried.
        seed_session(channel="C123", cli="claude")
        looked_up = []
        self.app.client.conversations_info = lambda **kw: looked_up.append(kw) or {
            "channel": {"id": kw["channel"], "name": "irrelevant"}
        }
        with mock.patch.object(bridge, "send_and_wait", return_value="ok"):
            self.handle("U", "C123", "hello", None)
        self.assertEqual(looked_up, [], "should not call conversations_info "
                         "when session already exists")


class DebugCommandTests(unittest.TestCase):
    """`!debug` dumps internal state — pid, uptime, sessions, orphan tmux."""

    def setUp(self):
        self.handle, self.app = fresh_handler()

    def test_dumps_empty_state(self):
        with mock.patch.object(bridge.subprocess, "check_output",
                               return_value=""):
            self.handle("U", "D1", "!debug", None)
        text = last_post_text(self.app)
        self.assertIn("Bridge debug", text)
        self.assertIn("pid", text)
        self.assertIn("In-memory sessions:* none", text)

    def test_dumps_seeded_sessions(self):
        seed_session(channel="C0001", cli="gemini",
                     tmux_name="gemini-proj-abcd")
        seed_session(channel="C0002", cli="codex",
                     tmux_name="codex-other-1234")
        with mock.patch.object(bridge.subprocess, "check_output",
                               return_value="gemini-proj-abcd\ncodex-other-1234"):
            self.handle("U", "D1", "!debug", None)
        text = last_post_text(self.app)
        self.assertIn("In-memory sessions (2):", text)
        self.assertIn("gemini-proj-abcd", text)
        self.assertIn("codex-other-1234", text)
        # Both tmux sessions are tracked, so no orphans.
        self.assertNotIn("Orphan", text)

    def test_lists_orphan_tmux_sessions(self):
        seed_session(channel="C0001", cli="gemini",
                     tmux_name="gemini-tracked-1111")
        # tmux ls returns the tracked one + an orphan that's not in `sessions`.
        with mock.patch.object(bridge.subprocess, "check_output",
                               return_value="gemini-tracked-1111\n"
                                            "gemini-orphan-2222\n"
                                            "some-random-name"):
            self.handle("U", "D1", "!debug", None)
        text = last_post_text(self.app)
        self.assertIn("Orphan agent-pattern tmux sessions", text)
        self.assertIn("gemini-orphan-2222", text)
        # Random name doesn't match the agent-channel regex → not listed
        # under orphans.
        self.assertNotIn("some-random-name", text)


# ---- safety: codex must use -a untrusted ------------------------------------

class CodexSafetyConfigTests(unittest.TestCase):
    """Guards against quietly loosening codex's approval mode.

    `-a on-request` lets the model decide when to ask, which in practice means
    explicit user phrases like "delete X" can run without a permission dialog
    being raised — and therefore without the bridge forwarding one to Slack.
    `-a untrusted` is a deliberate safety choice: only a small allowlist of
    read-only commands auto-approves; everything else asks. Don't loosen this
    without thinking through the bridge's "ask in Slack" promise.
    """

    def test_codex_launched_with_untrusted_approval(self):
        cmd = bridge.CLI_CONFIGS["codex"]["cmd"]
        self.assertIn("-a untrusted", cmd,
                      "codex must run with -a untrusted so model-initiated "
                      "mutations always ask before executing")
        self.assertNotIn("-a on-request", cmd)
        self.assertNotIn("--dangerously", cmd)

    def test_codex_sandboxes_workspace_writes(self):
        cmd = bridge.CLI_CONFIGS["codex"]["cmd"]
        self.assertIn("-s workspace-write", cmd,
                      "codex must run with -s workspace-write so file writes "
                      "are sandboxed to the session cwd")


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
