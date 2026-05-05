"""Drive the Slack bridge end-to-end as a real human user.

Reads SLACK_USER_TOKEN (xoxp-…) from .env and uses it to:
  1. Auto-create a fresh private channel like #codex-test_20260505_103015
  2. Invite the bridge bot
  3. Walk through the test plan: !sessions, typo guard, named-session race
     fix, simple round-trip with the real CLI, !cancel + !end
  4. Archive the test channel(s) at the end (override with --keep)

Real Slack messages, real bot replies, real CLI sessions. This catches
Slack-API-layer issues that the in-process sim (tests/sim_user.py) can't.

Examples:
  python3 tests/slack_drive.py                        # codex, in this repo's cwd
  python3 tests/slack_drive.py --cli gemini           # gemini
  python3 tests/slack_drive.py --cli gemini --keep    # don't archive at end
  python3 tests/slack_drive.py --cwd /tmp/my-project  # custom cwd
  python3 tests/slack_drive.py --skip-roundtrip       # skip the API-cost step
"""
import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

try:
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
except ImportError:
    sys.exit("missing dep: pip install slack_sdk  (or use the project's venv)")


# ---- env loading ------------------------------------------------------------

def load_env():
    """Read .env at repo root + os.environ. os.environ wins on conflict."""
    env_path = ROOT / ".env"
    out = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("SLACK_USER_TOKEN", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"):
        if os.environ.get(k):
            out[k] = os.environ[k]
    return out


# ---- pretty output ----------------------------------------------------------

BAR = "─" * 72
PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
WARN = "\033[33mWARN\033[0m"


def header(s):
    print(f"\n{BAR}\n## {s}\n{BAR}")


def trunc(s, n=300):
    return s if len(s) <= n else s[:n] + f" …[+{len(s) - n}b]"


# ---- driver -----------------------------------------------------------------

class Driver:
    def __init__(self, user_token, bot_token):
        self.user = WebClient(token=user_token)
        self.bot = WebClient(token=bot_token)
        self.bot_user_id = self.bot.auth_test()["user_id"]
        self.me_id = self.user.auth_test()["user_id"]
        # Per-channel: timestamp of the last bot message we've already seen.
        # Lets wait_bot() filter out old replies when polling.
        self.cursor = {}
        # Channels we created and should archive at the end (unless --keep).
        self.created_channels = []

    def create_channel(self, name):
        # IMPORTANT: bot creates the channel, not the user.
        # Slack delivers `message.groups` events to a bot only for channels
        # the bot is a *creator/early member* of. When a user creates a
        # private channel and invites the bot afterwards, the bot ends up
        # listed as a member but the Socket Mode connection silently drops
        # events for that channel. (Verified empirically — see commit log.)
        # Creating it via bot token sidesteps the issue.
        resp = self.bot.conversations_create(name=name, is_private=True)
        ch = resp["channel"]
        self.created_channels.append(ch["id"])
        return ch["id"], ch["name"]

    def invite_user(self, channel):
        """Invite the human (the user_token holder) into the bot-created channel."""
        try:
            self.bot.conversations_invite(channel=channel, users=self.me_id)
        except SlackApiError as e:
            if e.response.get("error") not in ("already_in_channel", "user_already_in_channel"):
                raise

    def post(self, channel, text):
        """Post as the human user. Returns the message ts."""
        resp = self.user.chat_postMessage(channel=channel, text=text)
        # Don't update cursor here — cursor tracks bot replies only.
        return resp["ts"]

    def send_and_wait(self, channel, text, *, max_attempts=1, wait_each=90,
                      contains=None):
        """Post `text` and wait for the bot's reply. Retries if no reply
        comes — Slack sometimes drops user-token-posted messages in a
        fresh channel. Only safe for idempotent commands."""
        for attempt in range(1, max_attempts + 1):
            try:
                post_ts = self.post(channel, text)
            except SlackApiError as e:
                err = e.response.get("error", "?")
                print(f"    [post failed: {err}]")
                return None
            m = self.wait_bot(channel, timeout=wait_each, after=post_ts,
                              contains=contains)
            if m:
                if attempt > 1:
                    print(f"    [recovered on attempt {attempt}/{max_attempts}]")
                return m
            if attempt < max_attempts:
                print(f"    [no reply within {wait_each}s; "
                      f"retry {attempt + 1}/{max_attempts}…]")
        return None

    def wait_bot(self, channel, *, timeout=30, contains=None, after=None):
        """Wait for the next bot message in `channel` whose ts is strictly
        greater than `after` (a Slack ts string OR a float seconds value).
        If `after` is None, defaults to "now" — useful when waiting for a
        delayed bot post that doesn't immediately follow a user post.
        Returns the message dict or None on timeout.

        Polls full history (no `oldest` filter) to avoid Slack's quirky
        string-comparison of `oldest` with sub-second-precision floats.
        """
        deadline = time.time() + timeout
        after_f = float(after) if after is not None else time.time()
        while time.time() < deadline:
            try:
                hist = self.user.conversations_history(channel=channel, limit=30)
            except SlackApiError as e:
                err = e.response.get("error", "?")
                print(f"    [history error: {err}]")
                return None
            # API returns newest-first; oldest-first traversal lets us
            # return the first qualifying reply (deterministic).
            for msg in reversed(hist.get("messages", [])):
                # Two flavors of bot messages:
                #   1. Regular bot post: user == bot_user_id, subtype unset.
                #   2. Branded bot post (chat:write.customize): subtype ==
                #      "bot_message", user is None, has username override
                #      ("CLI Bridge — Gemini" etc).
                # User-token posts carry bot_id too (same OAuth app) but
                # have user==me_id and subtype unset, so this filter
                # correctly excludes them.
                u = msg.get("user")
                is_regular_bot = (u == self.bot_user_id)
                is_branded_bot = (msg.get("subtype") == "bot_message")
                if not (is_regular_bot or is_branded_bot):
                    continue
                # Must be after the user-post we're waiting on a reply to.
                if float(msg.get("ts", "0")) <= after_f:
                    continue
                if contains and contains not in msg.get("text", ""):
                    continue
                return msg
            time.sleep(2.0)
        return None

    def find_new_channel(self, after_ts, name_prefix, timeout=30):
        """Poll users.conversations until we find a private channel whose
        name starts with `name_prefix` and was created after `after_ts`
        (unix seconds). Used to detect the agent channel that the bridge
        creates in response to `!<cli> <name> <path>`."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                lst = self.user.users_conversations(
                    types="private_channel", limit=200, exclude_archived=True)
            except SlackApiError as e:
                print(f"    [list error: {e.response.get('error')}]")
                break
            for ch in lst.get("channels", []):
                if (ch.get("name", "").startswith(name_prefix)
                        and ch.get("created", 0) >= after_ts):
                    return ch["id"], ch["name"]
            time.sleep(1.0)
        return None, None

    def archive(self, channel):
        try:
            self.user.conversations_archive(channel=channel)
        except SlackApiError as e:
            err = e.response.get("error", "?")
            if err == "already_archived":
                return
            print(f"    [archive {channel}: {err}]")


# ---- the actual test plan ---------------------------------------------------

def run_tests(d, args):
    ts_label = time.strftime("%Y%m%d_%H%M%S")
    entry_name = f"{args.cli}-test_{ts_label}"
    entry_text = entry_name  # may differ from final if Slack mangles

    header(f"creating entry channel #{entry_name}")
    entry_ch, final_name = d.create_channel(entry_name)
    print(f"    entry channel id = {entry_ch} (name=#{final_name})")
    d.invite_user(entry_ch)
    print(f"    user invited")
    # Passive sleep — let Slack's Socket Mode propagation finish before we
    # post anything. Empirically a 20s passive wait works reliably; active
    # probing (posting !sessions repeatedly during warmup) seems to confuse
    # Slack's event delivery and causes subsequent messages to drop.
    print(f"    waiting 20s for Slack subscription to settle…")
    time.sleep(20)
    d.cursor[entry_ch] = str(time.time())

    results = []

    def record(name, ok, detail=""):
        results.append((name, ok, detail))
        marker = PASS if ok else FAIL
        print(f"    {marker} {name}{(' — ' + detail) if detail else ''}")

    # ─────────────────────────────────────────────────────────────────
    header("STEP 1 — !sessions on empty bridge state (in entry channel)")
    # ─────────────────────────────────────────────────────────────────
    m = d.send_and_wait(entry_ch, "!sessions", max_attempts=1, wait_each=90)
    text = m.get("text", "") if m else "(no reply)"
    ok = "No active sessions" in text or "active session" in text
    record("!sessions in entry channel returns a session list/empty",
           ok, trunc(text, 150))

    # ─────────────────────────────────────────────────────────────────
    header("STEP 2 — typo guard: !session must NOT be forwarded to a CLI")
    # ─────────────────────────────────────────────────────────────────
    m = d.send_and_wait(entry_ch, "!session", max_attempts=1, wait_each=90)
    text = m.get("text", "") if m else "(no reply)"
    ok = "Unknown bridge command" in text and "!session" in text
    record("typo `!session` is intercepted with help message", ok, trunc(text, 200))

    # ─────────────────────────────────────────────────────────────────
    header(f"STEP 3 — race fix: post `!{args.cli} {args.name} {args.cwd}` and "
           f"hit `!sessions` in the new channel before tmux boot finishes")
    # ─────────────────────────────────────────────────────────────────
    before = int(time.time())
    cli_post_ts = d.post(entry_ch, f"!{args.cli} {args.name} {args.cwd}")
    new_id, new_name = d.find_new_channel(
        before, f"{args.cli}-{args.name}-", timeout=30)
    if not new_id:
        record("bridge creates named agent channel", False,
               "no new channel appeared within 30s")
        return results, entry_ch, None
    record("bridge creates named agent channel", True, f"#{new_name}")
    # The agent channel is freshly-created, so the same Slack subscription
    # propagation lag applies. Passive sleep instead of active warmup —
    # active probing seems to make subsequent message delivery worse.
    print("    waiting 20s for agent-channel subscription to settle…")
    time.sleep(20)
    d.cursor[new_id] = str(time.time())

    # Verify !sessions returns this session. Tolerant assertion: the
    # bridge may already have orphan sessions from previous runs; we just
    # check that the new agent channel id appears in the list.
    m = d.send_and_wait(new_id, "!sessions", contains="active session",
                        max_attempts=1, wait_each=90)
    text = m.get("text", "") if m else "(no reply)"
    ok = bool(m) and "active session" in text and new_id in text
    record("!sessions returns the named session", ok, trunc(text, 300))

    # Wait for the "ready" post that comes after tmux finishes booting.
    # The "ready in" message is posted by the bridge ~30s after the
    # !gemini command (when tmux finishes booting) — likely DURING our
    # 20s pre-warm sleep, so the floor must be the !gemini post ts (not
    # "now") otherwise we'll miss a message that's already in the past.
    m = d.wait_bot(new_id, contains="ready in", timeout=120, after=cli_post_ts)
    if m:
        record("bridge posts ready message after boot", True, trunc(m.get("text", ""), 120))
    else:
        record("bridge posts ready message after boot", False, "no `ready in` within 120s")
        # Bail before we try to drive a non-existent agent.
        return results, entry_ch, new_id

    # ─────────────────────────────────────────────────────────────────
    header("STEP 4 — typo guard inside agent channel")
    # ─────────────────────────────────────────────────────────────────
    m = d.send_and_wait(new_id, "!session", max_attempts=1, wait_each=90)
    text = m.get("text", "") if m else "(no reply)"
    ok = "Unknown bridge command" in text
    record("typo intercepted in agent channel (NOT forwarded to CLI)",
           ok, trunc(text, 200))

    # ─────────────────────────────────────────────────────────────────
    if args.skip_roundtrip:
        header("STEP 5 — skipped (--skip-roundtrip)")
    else:
        header("STEP 5 — round-trip: ask the agent to reply with READY")
        # ─────────────────────────────────────────────────────────────────
        m = d.send_and_wait(
            new_id,
            "Reply with exactly the single word READY and nothing else.",
            max_attempts=1, wait_each=120)
        text = m.get("text", "") if m else "(no reply)"
        ok = "READY" in text.upper()
        record("agent round-trip", ok, trunc(text, 250))

    # ─────────────────────────────────────────────────────────────────
    header("STEP 6 — !cancel then !end (bridge should archive the agent channel)")
    # ─────────────────────────────────────────────────────────────────
    m = d.send_and_wait(new_id, "!cancel", max_attempts=2, wait_each=15,
                        contains="interrupt")
    record("!cancel sent", bool(m),
           trunc(m.get("text", "") if m else "(no reply)", 150))

    # max_attempts=1 because the bridge archives the channel on the first
    # !end, so retrying the post would fail with is_archived.
    m = d.send_and_wait(new_id, "!end", max_attempts=1, wait_each=30,
                        contains="ended")
    record("!end posted (bridge will archive this channel)",
           bool(m), trunc(m.get("text", "") if m else "(no reply)", 150))

    return results, entry_ch, new_id


# ---- main -------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--cli", choices=("codex", "gemini", "claude"), default="codex",
                   help="which CLI the named session uses (default: codex)")
    p.add_argument("--name", default="drivetest",
                   help="project name passed to the bridge (default: drivetest)")
    p.add_argument("--cwd", default=str(ROOT),
                   help=f"cwd for the agent (default: {ROOT})")
    p.add_argument("--keep", action="store_true",
                   help="don't archive channels at end (handy for debugging)")
    p.add_argument("--skip-roundtrip", action="store_true",
                   help="skip the agent round-trip step (saves API quota)")
    args = p.parse_args()

    if not Path(args.cwd).is_dir():
        sys.exit(f"--cwd {args.cwd} is not a directory")

    env = load_env()
    user_token = env.get("SLACK_USER_TOKEN")
    bot_token = env.get("SLACK_BOT_TOKEN")
    if not user_token or not user_token.startswith("xoxp-"):
        sys.exit("SLACK_USER_TOKEN missing or not xoxp- in .env (and not in os.environ)")
    if not bot_token or not bot_token.startswith("xoxb-"):
        sys.exit("SLACK_BOT_TOKEN missing or not xoxb- in .env (and not in os.environ)")

    d = Driver(user_token, bot_token)
    print(f"[i] driving as user_id={d.me_id}, bot_id={d.bot_user_id}")
    print(f"[i] cli={args.cli}  name={args.name}  cwd={args.cwd}  keep={args.keep}")

    results = []
    entry_ch = agent_ch = None
    try:
        results, entry_ch, agent_ch = run_tests(d, args)
    except KeyboardInterrupt:
        print("\n[!] interrupted")
    finally:
        # Cleanup. Bridge auto-archives the agent channel on !end; we still
        # archive the entry channel here. If --keep, we leave both.
        if args.keep:
            print(f"\n[i] --keep: not archiving anything")
        else:
            header("CLEANUP — archiving channels")
            for ch in d.created_channels:
                d.archive(ch)
                print(f"    archived {ch}")
            # Best-effort archive of the agent channel too — usually the
            # bridge already did this on !end, so this'll be a no-op.
            if agent_ch:
                d.archive(agent_ch)
                print(f"    (also tried agent channel {agent_ch})")

    # ---- Summary ----
    header("SUMMARY")
    if not results:
        print("    (no results — test bailed early)")
        sys.exit(1)
    n_pass = sum(1 for _, ok, _ in results if ok)
    n_fail = len(results) - n_pass
    for name, ok, detail in results:
        marker = PASS if ok else FAIL
        print(f"    {marker} {name}")
    print(f"\n    {n_pass}/{len(results)} passed, {n_fail} failed")
    sys.exit(0 if n_fail == 0 else 1)


if __name__ == "__main__":
    main()
