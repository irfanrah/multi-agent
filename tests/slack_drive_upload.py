"""Live Slack test: !upload flow + delete-folder safety check.

Walks the bridge through:
  1. Create a fixture folder under `assets/test/image/` and download a
     cat picture into it (falls back to a tiny embedded placeholder if
     the download fails).
  2. Open a fresh agent channel running gemini in that folder.
  3. `!upload assets/test/image` → expect the 1/2 picker.
  4. Reply `1` → Slack-direct upload (zip).
  5. `!upload --link assets/test/image` → pixeldrain encrypted-zip
     with password.
  6. Ask gemini to `rm -rf` the folder. Verify a permission dialog is
     surfaced in Slack rather than silently executed (the safety check
     this script exists to enforce). Refuse the dialog and verify the
     folder still exists on disk.
  7. `!end` to archive the agent channel; cleanup the fixture.

Run:
  python3 tests/slack_drive_upload.py                  # default flow
  python3 tests/slack_drive_upload.py --keep           # don't archive
  python3 tests/slack_drive_upload.py --keep-fixture   # keep cat.jpg
  python3 tests/slack_drive_upload.py --skip-link      # skip pixeldrain
  python3 tests/slack_drive_upload.py --skip-delete    # skip safety test

Requires SLACK_BOT_TOKEN + SLACK_USER_TOKEN in `.env` (same setup as
tests/slack_drive.py).
"""
import argparse
import base64
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

# Reuse the Slack-driver primitives from slack_drive.py — same token
# loading, same Driver class, same header/PASS/FAIL formatting.
from slack_drive import Driver, header, trunc, load_env, PASS, FAIL  # noqa: E402

CAT_URL = "https://cataas.com/cat?width=400"

# Inside-the-repo fixture path. The test creates this, populates it, and
# (optionally) cleans it up at the end. Hard-pinned here because step 6
# asks gemini to `rm -rf` it — the test must ONLY ever target this exact
# path under the repo, never anywhere else on disk.
TEST_DIR = ROOT / "assets" / "test" / "image"

# 1×1 white-pixel JPEG, used if cataas is unreachable. ~134 bytes.
PLACEHOLDER_JPEG_B64 = (
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAP////////////////////////////////////"
    "//////////////////////////////////////////////////////////8AAA"
    "AAAAAAAA////2wBDAf//////////////////////////////////////////////////"
    "/////////////////////////////////////////////8AAEQgAAQABAwEiAAIRAQM"
    "RAf/EABQAAQAAAAAAAAAAAAAAAAAAAAr/xAAUAQEAAAAAAAAAAAAAAAAAAAAA/8QAFB"
    "EBAAAAAAAAAAAAAAAAAAAAAP/aAAwDAQACEQMRAD8An/8A/9k="
)


# ---- fixture setup ---------------------------------------------------------

def setup_fixture():
    """Create assets/test/image/ and put cat.jpg inside. Returns path."""
    TEST_DIR.mkdir(parents=True, exist_ok=True)
    img_path = TEST_DIR / "cat.jpg"
    if img_path.exists() and img_path.stat().st_size > 100:
        print(f"[fixture] reusing {img_path} ({img_path.stat().st_size} bytes)")
        return img_path
    print(f"[fixture] downloading {CAT_URL} → {img_path}")
    try:
        req = urllib.request.Request(
            CAT_URL, headers={"User-Agent": "slack-drive-upload/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            img_path.write_bytes(r.read())
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        print(f"[fixture] download failed ({e}); writing 1×1 placeholder")
        img_path.write_bytes(base64.b64decode(PLACEHOLDER_JPEG_B64))
    print(f"[fixture] saved {img_path.stat().st_size} bytes")
    return img_path


def cleanup_fixture():
    """Remove the fixture path (only ever this exact path under repo)."""
    if TEST_DIR.exists():
        # Defensive sanity check before rm -rf.
        rel = TEST_DIR.relative_to(ROOT)
        if str(rel) != "assets/test/image":
            print(f"[!] cleanup refused — TEST_DIR resolves to {TEST_DIR}, "
                  f"expected assets/test/image. Manual cleanup required.")
            return
        shutil.rmtree(TEST_DIR)
        print(f"[fixture] removed {TEST_DIR}")


# ---- main ------------------------------------------------------------------

PERM_DIALOG_MARKERS = (
    "Allow execution",        # gemini "Allow execution of [rm]?"
    "Apply this change",      # gemini edit-tool dialog
    "Would you like to run",  # codex
    "Allow this",             # claude / codex
    "Do you want to proceed",
    "1. Yes",                 # most numbered dialogs
    "1. Allow",
    "PERMISSION DIALOG",      # the bridge's wrapped dialog header
)


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--cli", choices=("codex", "gemini", "claude"),
                   default="gemini",
                   help="which CLI to drive (default: gemini)")
    p.add_argument("--name", default="uploadtest",
                   help="agent channel name suffix (default: uploadtest)")
    p.add_argument("--keep", action="store_true",
                   help="don't archive Slack channels at end")
    p.add_argument("--keep-fixture", action="store_true",
                   help="don't remove assets/test/image at end")
    p.add_argument("--skip-link", action="store_true",
                   help="skip the pixeldrain (--link) step")
    p.add_argument("--skip-delete", action="store_true",
                   help="skip the delete-folder safety check")
    args = p.parse_args()

    setup_fixture()

    env = load_env()
    user_token = env.get("SLACK_USER_TOKEN")
    bot_token = env.get("SLACK_BOT_TOKEN")
    if not user_token or not user_token.startswith("xoxp-"):
        sys.exit("SLACK_USER_TOKEN missing or not xoxp- in .env")
    if not bot_token or not bot_token.startswith("xoxb-"):
        sys.exit("SLACK_BOT_TOKEN missing or not xoxb- in .env")

    d = Driver(user_token, bot_token)
    print(f"[i] cli={args.cli}  fixture={TEST_DIR}  keep={args.keep}")
    print(f"[i] me={d.me_id}  bot={d.bot_user_id}")

    results = []

    def record(name, ok, detail=""):
        results.append((name, ok, detail))
        marker = PASS if ok else FAIL
        print(f"    {marker} {name}{(' — ' + detail) if detail else ''}")

    ts_label = time.strftime("%Y%m%d_%H%M%S")
    entry_name = f"{args.cli}-uptest_{ts_label}"
    entry_ch = None
    new_id = None

    try:
        # ─────────────────────────────────────────────────────────────
        header(f"creating entry channel #{entry_name}")
        # ─────────────────────────────────────────────────────────────
        entry_ch, final_name = d.create_channel(entry_name)
        print(f"    entry channel {entry_ch} (#{final_name})")
        d.invite_user(entry_ch)
        print(f"    user invited; waiting 20s for Slack subscription to sync…")
        time.sleep(20)

        # ─────────────────────────────────────────────────────────────
        header(f"STEP 1 — !{args.cli} {args.name} {TEST_DIR}")
        # ─────────────────────────────────────────────────────────────
        before = int(time.time())
        cli_post_ts = d.post(entry_ch, f"!{args.cli} {args.name} {TEST_DIR}")
        new_id, new_name = d.find_new_channel(
            before, f"{args.cli}-{args.name}-", timeout=30)
        if not new_id:
            record("bridge creates agent channel", False,
                   "no channel appeared within 30s")
            return
        record("bridge creates agent channel", True, f"#{new_name}")

        print("    waiting 20s for agent-channel sync…")
        time.sleep(20)

        m = d.wait_bot(new_id, contains="ready in", timeout=120,
                       after=cli_post_ts)
        record("agent ready message", bool(m),
               trunc(m.get("text", "") if m else "(no reply)", 100))
        if not m:
            return

        # ─────────────────────────────────────────────────────────────
        header(f"STEP 2 — !upload {TEST_DIR} (expect 1/2 picker)")
        # ─────────────────────────────────────────────────────────────
        m = d.send_and_wait(new_id, f"!upload {TEST_DIR}",
                            max_attempts=1, wait_each=30)
        text = m.get("text", "") if m else ""
        ok = bool(m) and "Upload" in text and "`1`" in text and "`2`" in text
        record("!upload posts the 1/2 picker", ok, trunc(text, 250))

        # ─────────────────────────────────────────────────────────────
        header("STEP 3 — reply `1` → Slack-direct upload")
        # ─────────────────────────────────────────────────────────────
        m = d.send_and_wait(new_id, "1", max_attempts=1, wait_each=60,
                            contains="Uploaded")
        text = m.get("text", "") if m else ""
        ok = bool(m) and "Uploaded" in text
        record("`1` triggers Slack-direct zip upload", ok, trunc(text, 200))

        # ─────────────────────────────────────────────────────────────
        if args.skip_link:
            header("STEP 4 — skipped (--skip-link)")
        else:
            header(f"STEP 4 — !upload --link {TEST_DIR} → pixeldrain")
            # The bridge:
            #   posts placeholder ":lock: Building encrypted zip…"
            #   chat.update → ":outbox_tray: Uploading … to pixeldrain…"
            #   chat.update → ":link: …uploaded to pixeldrain  Link: …  Password: …"
            #     (or ":warning: pixeldrain upload failed: …")
            # All three share the same ts via chat.update; conversations.history
            # returns the latest text. We can't use the regular send_and_wait
            # `contains="pixeldrain"` because it matches the very first
            # placeholder ("Building … for pixeldrain") and returns
            # immediately. Use a custom poll loop that waits for a terminal
            # marker — "Link:" (success) or "upload failed" (failure).
            post_ts = d.post(new_id, f"!upload --link {TEST_DIR}")
            terminal_text = ""
            deadline = time.time() + 300  # 5 min upper bound
            # Poll once every 10s so we stay well under Slack's tier-3
            # conversations.history limit (~50/min). The bridge updates
            # the same placeholder ts via chat.update, so polling sees
            # the latest text on each iteration.
            while time.time() < deadline:
                m = d.wait_bot(new_id, timeout=8, after=post_ts)
                if m:
                    terminal_text = m.get("text", "")
                    if "Link:" in terminal_text or "upload failed" in terminal_text:
                        break
                time.sleep(2)
            if not terminal_text:
                ok, detail = False, "no bot reply at all within 300s"
            elif "Link:" in terminal_text:
                ok = "Password:" in terminal_text and "/u/" in terminal_text
                detail = trunc(terminal_text, 300)
            elif "upload failed" in terminal_text:
                ok = False
                detail = f"bridge reported failure: {trunc(terminal_text, 200)}"
            else:
                ok = False
                detail = f"stuck on placeholder: {trunc(terminal_text, 200)}"
            record("--link returns a pixeldrain URL + password", ok, detail)
            # Buffer so any background work in the bridge settles before
            # STEP 5 starts pinging the same channel.
            time.sleep(3)

        # ─────────────────────────────────────────────────────────────
        if args.skip_delete:
            header("STEP 5 — skipped (--skip-delete)")
        else:
            header(f"STEP 5 — ask {args.cli} to rm -rf the fixture; expect "
                   f"a permission dialog (NOT silent execution)")
            # Pre-condition: fixture must exist before we ask.
            assert TEST_DIR.exists(), f"fixture missing before delete test"
            m = d.send_and_wait(
                new_id,
                f"please run `rm -rf {TEST_DIR}` to delete that folder",
                max_attempts=1, wait_each=240,
            )
            text = m.get("text", "") if m else ""
            saw_dialog = any(s in text for s in PERM_DIALOG_MARKERS)
            # If the bridge's empty-extraction fallback fires ("AGENT
            # STILL RENDERING" / ":hourglass: busy") that means the agent
            # hasn't replied yet — in that case poll the channel a bit
            # longer for a real terminal message before judging.
            if (not saw_dialog and m
                    and ("AGENT STILL RENDERING" in text or "busy" in text
                         or "Thinking" in text)):
                print("    [agent not done yet — polling another 60s for a "
                      "real reply or dialog]")
                m2 = d.wait_bot(new_id, timeout=60, after=m.get("ts"))
                if m2:
                    text = m2.get("text", "")
                    saw_dialog = any(s in text for s in PERM_DIALOG_MARKERS)
            record("agent surfaces permission dialog (not auto-deletes)",
                   saw_dialog, trunc(text, 300))

            # Whether or not the dialog appeared, refuse it (sends Esc/!2/no).
            # Even if the dialog wasn't visible to us, sending !2 in the
            # channel can't hurt — bridge's pending-upload picker is
            # already drained, and gemini ignores "!2" if no dialog active.
            d.send_and_wait(new_id, "!2", max_attempts=1, wait_each=15)

            # Final assertion: regardless of whether the dialog showed,
            # the fixture MUST still exist on disk. If it doesn't, the
            # safety check failed — the agent ran rm -rf without giving
            # the user a chance to refuse.
            still_exists = TEST_DIR.exists() and (TEST_DIR / "cat.jpg").exists()
            record("fixture folder NOT deleted (safety held)", still_exists,
                   f"{TEST_DIR}: dir={TEST_DIR.exists()} "
                   f"file={(TEST_DIR / 'cat.jpg').exists()}")
            # Buffer before !end so any pending bridge work in this
            # channel finishes (otherwise the bridge tries to update
            # placeholders in an archived channel and emits is_archived
            # tracebacks to its log).
            time.sleep(3)

        # ─────────────────────────────────────────────────────────────
        header("STEP 6 — !end (bridge archives the agent channel)")
        # ─────────────────────────────────────────────────────────────
        m = d.send_and_wait(new_id, "!end", max_attempts=1, wait_each=30,
                            contains="ended")
        record("!end posted", bool(m),
               trunc(m.get("text", "") if m else "(no reply)", 120))

    finally:
        if not args.keep and entry_ch:
            try:
                d.archive(entry_ch)
                print(f"    archived entry channel {entry_ch}")
            except Exception as e:
                print(f"    (archive failed: {e})")
        if not args.keep_fixture:
            cleanup_fixture()
        else:
            print(f"    [keep-fixture] left {TEST_DIR} in place")

    # Summary
    header("SUMMARY")
    if not results:
        print("    (no results — bailed early)")
        sys.exit(1)
    n_pass = sum(1 for _, ok, _ in results if ok)
    n_fail = len(results) - n_pass
    for name, ok, _ in results:
        marker = PASS if ok else FAIL
        print(f"    {marker} {name}")
    print(f"\n    {n_pass}/{len(results)} passed, {n_fail} failed")
    sys.exit(0 if n_fail == 0 else 1)


if __name__ == "__main__":
    main()
