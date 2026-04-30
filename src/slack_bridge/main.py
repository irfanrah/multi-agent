"""Slack <-> CLI bridge with stateful tmux sessions.

A Slack DM (or channel where the bot is invited) becomes a persistent chat with
Gemini CLI, Codex CLI, or Claude Code. Each (user, channel) keeps its own tmux
session so context persists across messages.

Slack commands:
  !gemini     Start (or restart) a Gemini session
  !codex      Start (or restart) a Codex session
  !claude     Start (or restart) a Claude session
  !end        End current session
  !status     Show what's active
  !reset      Same as !end + relaunch with same CLI
  <anything>  Forwarded to the active CLI session

Required env:
  SLACK_BOT_TOKEN   xoxb-...   (bot user OAuth token)
  SLACK_APP_TOKEN   xapp-...   (app-level token, scope connections:write)

Required Slack scopes: app_mentions:read, chat:write, im:history, im:read,
im:write. Subscribe to events: message.im, app_mention.
"""
import importlib.util
import os
import re
import secrets
import sys
import time
import threading
import subprocess
from dataclasses import dataclass, field
from typing import Optional

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.errors import SlackApiError


def _load_check_limit():
    """Load src/check_limit/main.py as a distinct module (avoids name collision)."""
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.normpath(os.path.join(here, "..", "check_limit", "main.py"))
    spec = importlib.util.spec_from_file_location("check_limit_main", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_check_limit = None  # lazy-loaded

# ---- terminal cleanup --------------------------------------------------------

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[=>]")
BOX_RE = re.compile(r"[│╭╯╰╮─█░▝▘▛▜▟▞▖▗▎▏▬▀▔▁┌┐└┘├┤┬┴┼═║╔╗╚╝╠╣╦╩╬]")
SPINNER_CHARS = set("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏◐◑◒◓◴◷◶◵|/-\\")
PROMPT_PREFIXES = ("›", "❯", ">_", ">>", ">", "•")


def clean_output(text):
    text = ANSI_RE.sub("", text)
    text = text.replace("\r", "")
    out_lines = []
    for raw in text.split("\n"):
        line = BOX_RE.sub("", raw).rstrip()
        stripped = line.strip()
        if not stripped:
            out_lines.append("")
            continue
        # Drop spinner-only lines (just ⠋ etc, possibly with single status word).
        if all(c in SPINNER_CHARS or c.isspace() for c in stripped):
            continue
        # Drop empty prompt lines like "›" or "❯ ".
        if any(stripped == p or stripped == p + " " for p in PROMPT_PREFIXES):
            continue
        out_lines.append(line)
    # Collapse multiple blank lines.
    collapsed, blank = [], False
    for line in out_lines:
        if not line.strip():
            if not blank:
                collapsed.append("")
            blank = True
        else:
            collapsed.append(line)
            blank = False
    return "\n".join(collapsed).strip()


# ---- tmux helpers ------------------------------------------------------------

def _tmux(*args, capture=False):
    if capture:
        return subprocess.check_output(["tmux", *args], text=True)
    return subprocess.run(["tmux", *args], stderr=subprocess.DEVNULL).returncode


def session_exists(name):
    return subprocess.run(
        ["tmux", "has-session", "-t", name], stderr=subprocess.DEVNULL,
    ).returncode == 0


def capture(name):
    """Full capture including scrollback so diffs work after the pane has scrolled."""
    return _tmux("capture-pane", "-pt", name, "-S", "-3000", capture=True).rstrip()


# ---- CLI session lifecycle ---------------------------------------------------

CLI_CONFIGS = {
    # `ready_marker`: appears once the CLI's input prompt is ready.
    # `assistant_marker`: prefix the CLI uses for assistant turns in the conversation log.
    # `cmd`: launched via tmux; flags are tuned so the CLI doesn't sit on permission prompts
    # (which the bridge can't answer) and so output stays in scrollback (no alt-screen).
    # `trust_pattern` + `trust_keys`: substring to detect first-launch folder-trust dialog
    # and the keystrokes to send to accept it. start_session auto-handles this so users
    # don't have to manually trust each new project folder.
    "gemini": {
        "cmd": "gemini",
        "ready_marker": "Type your message",
        "assistant_marker": "✦",
        "trust_pattern": "Do you trust the files in this folder",
        "trust_keys": "1",  # "1. Trust folder"
    },
    "codex": {
        "cmd": "codex --no-alt-screen -s workspace-write",
        "ready_marker": "›",
        "assistant_marker": "•",
    },
    "claude": {
        "cmd": "claude",
        "ready_marker": "? for shortcuts",
        "assistant_marker": "●",
        "trust_pattern": "Yes, I trust this folder",
        "trust_keys": "1",  # "1. Yes, I trust this folder"
    },
}


@dataclass
class CLISession:
    cli: str
    tmux_name: str
    last_used: float = field(default_factory=time.time)
    path: Optional[str] = None
    slack_channel_id: Optional[str] = None
    is_named: bool = False  # True when this session owns its dedicated Slack channel
    # Lock taken while a message is being typed/processed for this session.
    # Prevents two concurrent Slack messages from interleaving keystrokes.
    io_lock: threading.Lock = field(default_factory=threading.Lock)


# Keyed by Slack channel_id. DM channels are unique per user, so this also
# uniquely identifies per-user DM sessions; named sessions live in their own channel.
sessions: dict = {}
sessions_lock = threading.Lock()
IDLE_TIMEOUT_SEC = 30 * 60


def start_session(cli, name, max_wait=30, cwd=None):
    cfg = CLI_CONFIGS[cli]
    if session_exists(name):
        _tmux("kill-session", "-t", name)
    args = ["new-session", "-d", "-s", name, "-x", "200", "-y", "50"]
    if cwd:
        args += ["-c", cwd]
    args.append(cfg["cmd"])
    _tmux(*args)

    trust_pattern = cfg.get("trust_pattern")
    trust_keys = cfg.get("trust_keys")
    trust_handled = False

    deadline = time.time() + max_wait
    while time.time() < deadline:
        text = capture(name)
        if cfg["ready_marker"] in text:
            return True
        # First-launch folder-trust dialog: dismiss it once.
        if not trust_handled and trust_pattern and trust_pattern in text:
            _tmux("send-keys", "-t", name, "-l", trust_keys)
            time.sleep(0.3)
            _tmux("send-keys", "-t", name, "Enter")
            trust_handled = True
        time.sleep(0.5)
    return False


def _slugify_channel(name):
    """Slack channel names: lowercase, [a-z0-9_-], <=80 chars."""
    s = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-_")[:70]
    return s or "session"


def start_named_session(cli, name, path, inviter_user, app):
    """Create channel `agent-<slug>`, invite inviter, launch tmux with cwd=path.

    Returns (channel_id, tmux_name, channel_name) on success.
    Raises ValueError on bad path. Raises SlackApiError or RuntimeError on Slack/CLI failures.
    """
    if not os.path.isdir(path):
        raise ValueError(f"path not found or not a directory: `{path}`")

    slug = _slugify_channel(name)
    base_name = f"agent-{slug}"

    # Try create as private; on name collision, append a short random suffix.
    channel_id = None
    final_name = base_name
    last_err = None
    for attempt in range(3):
        try_name = base_name if attempt == 0 else f"{base_name}-{secrets.token_hex(2)}"
        try:
            resp = app.client.conversations_create(name=try_name, is_private=True)
            channel_id = resp["channel"]["id"]
            final_name = try_name
            break
        except SlackApiError as e:
            err = e.response.get("error", "")
            last_err = e
            if err == "name_taken":
                continue
            raise
    if channel_id is None:
        raise last_err or RuntimeError("could not create channel")

    app.client.conversations_invite(channel=channel_id, users=inviter_user)

    tmux_name = f"slack_named_{channel_id}_{cli}".replace(".", "_")
    # 90s instead of 30s — snap apps (gemini, codex) can be slow to start under
    # memory/CPU pressure. If we timeout, capture the pane to help diagnose.
    if not start_session(cli, tmux_name, cwd=path, max_wait=90):
        try:
            tail = capture(tmux_name)[-600:] or "(empty)"
        except Exception:
            tail = "(capture failed)"
        kill_session(tmux_name)
        try:
            app.client.conversations_archive(channel=channel_id)
        except SlackApiError:
            pass
        raise RuntimeError(
            f"`{cli}` didn't reach prompt within 90s in `{path}`. "
            f"This usually means the system is under heavy load.\n"
            f"Last pane content:\n```\n{tail}\n```"
        )

    return channel_id, tmux_name, final_name


def kill_session(name):
    if session_exists(name):
        _tmux("kill-session", "-t", name)


CHROME_DIVIDER_RE = re.compile(
    r"^.*\?\s+for\s+shortcuts.*$"        # Gemini/Claude help hint (may have trailing status)
    r"|^─{3,}.*$"                        # Horizontal divider (Gemini, Claude)
    r"|^▄{3,}\s*$"                       # Gemini box top
    r"|^▀{3,}\s*$"                       # Gemini box bottom
    r"|^\s*Shift\+Tab to accept edits\s*$"
    r"|^\s*›\s"                          # Codex input prompt (next placeholder)
    r"|^\s*❯\s*$",                       # Claude empty input prompt
    re.MULTILINE,
)
THINKING_RE = re.compile(r"⠋|⠙|⠹|⠸|⠼|⠴|⠦|⠧|⠇|⠏")


def _is_responding(text):
    """Heuristic: is the CLI still rendering/thinking?"""
    return ("Thinking" in text or "Working" in text or "esc to cancel" in text
            or bool(THINKING_RE.search(text)))


def send_and_wait(name, user_text, cli, max_wait=180, response_stable_secs=3.5,
                  idle_stable_secs=8.0, poll=1.0):
    """Send user_text + Enter, wait for the CLI to settle, return cleaned text.

    Two settle conditions; whichever fires first ends the wait:
      - response_stable_secs of stability with a NEW assistant marker visible
        (the CLI just produced an answer and is idle).
      - idle_stable_secs of stability without a new marker AND no spinner
        (the CLI is sitting at a permission prompt or other input dialog —
        forward it to Slack so the user can answer there).
    """
    cfg = CLI_CONFIGS[cli]
    marker = cfg["assistant_marker"]

    pre = capture(name)
    pre_marker_count = pre.count(marker)

    _tmux("send-keys", "-t", name, "-l", user_text)
    time.sleep(0.4)
    _tmux("send-keys", "-t", name, "Enter")

    last, response_stable_at, idle_stable_at = "", None, None
    deadline = time.time() + max_wait
    while time.time() < deadline:
        cur = capture(name)
        if cur == last and not _is_responding(cur):
            new_turn = cur.count(marker) > pre_marker_count
            if new_turn:
                if response_stable_at is None:
                    response_stable_at = time.time()
                elif time.time() - response_stable_at >= response_stable_secs:
                    break
            else:
                if idle_stable_at is None:
                    idle_stable_at = time.time()
                elif time.time() - idle_stable_at >= idle_stable_secs:
                    break
        else:
            response_stable_at = None
            idle_stable_at = None
            last = cur
        time.sleep(poll)

    post = capture(name)
    post_no_ansi = ANSI_RE.sub("", post)

    # Find the LAST assistant marker — that's this turn's response.
    last_idx = post_no_ansi.rfind(marker)
    if last_idx < 0:
        return clean_output(post_no_ansi)
    after = post_no_ansi[last_idx:]

    # Cut off at the input chrome that sits below the conversation.
    chrome = CHROME_DIVIDER_RE.search(after)
    if chrome:
        after = after[:chrome.start()]

    cleaned = clean_output(after)
    # Strip a leading marker char so the response reads naturally.
    if cleaned.startswith(marker):
        cleaned = cleaned[len(marker):].lstrip()
    return cleaned


def cleanup_idle_loop(app=None):
    """Periodically kill tmux sessions that have been idle past the timeout.
    For named sessions (with their own Slack channel), archive the channel too."""
    while True:
        time.sleep(60)
        now = time.time()
        with sessions_lock:
            stale_keys = [k for k, s in sessions.items() if now - s.last_used > IDLE_TIMEOUT_SEC]
            stale_sessions = [(k, sessions.pop(k)) for k in stale_keys]
        for _k, sess in stale_sessions:
            kill_session(sess.tmux_name)
            if app and sess.is_named and sess.slack_channel_id:
                try:
                    app.client.conversations_archive(channel=sess.slack_channel_id)
                except SlackApiError as e:
                    print(f"[idle-archive] failed: {e.response.get('error')}")


# ---- check_limit integration -------------------------------------------------

def run_check_limit():
    """Run the three usage captures in parallel; retry any CLI that didn't return
    enough rows. Returns (text_fallback, blocks)."""
    global _check_limit
    if _check_limit is None:
        _check_limit = _load_check_limit()
    cl = _check_limit

    targets = [
        ("Claude", "claude /usage", cl.parse_claude_rows, 3, None, None),
        ("Gemini", "gemini /model", cl.parse_gemini_rows, 3, None, None),
        ("Codex",  "codex",         cl.parse_codex_rows,  2, "/status", lambda t: "›" in t),
    ]

    def fetch(name, cmd, parser, expected, send_keys, prompt_ready, suffix="", max_wait=30):
        raw = cl.capture_cli_usage(
            cmd, f"slack_chk_{name.lower()}{suffix}",
            send_keys=send_keys,
            prompt_ready=prompt_ready,
            ready_check=lambda t: len(parser(t)) >= expected,
            max_wait=max_wait,
        )
        return parser(raw)

    results = {}
    lock = threading.Lock()

    def worker(t):
        name = t[0]
        try:
            rows = fetch(*t)
        except Exception as e:
            print(f"[check_limit] {name} parallel error: {e}")
            rows = []
        with lock:
            results[name] = rows

    # First pass: parallel.
    threads = [threading.Thread(target=worker, args=(t,), daemon=True) for t in targets]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    # Retry pass: any CLI that came back short of expected.
    for t in targets:
        name, _cmd, _parser, expected = t[0], t[1], t[2], t[3]
        if len(results.get(name, [])) < expected:
            print(f"[check_limit] {name} returned {len(results.get(name, []))}/{expected} rows, retrying sequentially")
            try:
                rows = fetch(*t, suffix="_retry", max_wait=20)
                if rows:
                    results[name] = rows
            except Exception as e:
                print(f"[check_limit] {name} retry error: {e}")

    return format_check_limit(results)


def format_check_limit(results):
    """Return (text_fallback, blocks) for chat_update."""
    from datetime import datetime
    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": "📊 AI CLI Usage", "emoji": True}},
        {"type": "context",
         "elements": [{"type": "mrkdwn",
                       "text": f"_Updated {datetime.now().strftime('%b %d, %H:%M')}_"}]},
    ]
    text_lines = ["AI CLI Usage"]
    for name in ("Claude", "Gemini", "Codex"):
        rows = results.get(name) or []
        blocks.append({"type": "divider"})
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn", "text": _section_md(name, rows)}})
        text_lines.append(f"\n{name}")
        for r in rows:
            text_lines.append(f"  {r['label']}: {r.get('pct_used', 0)}% used")
    return "\n".join(text_lines), blocks


def _section_md(name, rows):
    if not rows:
        return f"⚪ *{name}*\n_(no data)_"
    pcts = [r.get("pct_used", 0) for r in rows if r.get("pct_used", 0) >= 0]
    overall = max(pcts) if pcts else 0
    head = f"{_status_emoji(overall)} *{name}*"
    body_lines = [head]
    for r in rows:
        body_lines.append(_row_md(r))
    return "\n".join(body_lines)


def _row_md(r):
    pct = r.get("pct_used", 0)
    label = r["label"]
    if pct < 0:
        return f"  ⚠️ _{label}_"
    bar = _pct_bar(pct)
    pct_str = f"{pct:>3d}%"
    line = f"  `{pct_str}` {bar}  {label}"
    reset = _clean_reset(r.get("reset"))
    if reset:
        line += f"  _·  resets {reset}_"
    return line


def _status_emoji(pct):
    if pct >= 80: return "🔴"
    if pct >= 50: return "🟡"
    return "🟢"


def _pct_bar(pct, width=10):
    pct = max(0, min(100, int(pct)))
    filled = round(pct * width / 100)
    return f"`{'█' * filled}{'░' * (width - filled)}`"


def _clean_reset(reset):
    if not reset:
        return None
    # Drop redundant timezone tags like " (Asia/Seoul)"
    return re.sub(r"\s*\([A-Za-z]+/[\w_+-]+\)\s*$", "", reset).strip()


# ---- Slack glue --------------------------------------------------------------

def _format_for_slack(text):
    if not text:
        return "_(no output)_"
    if "\n" in text or len(text) > 80:
        return f"```\n{text[:38000]}\n```"
    return text


def make_handler(app):
    """Build the message handler bound to a slack_bolt App."""
    def post(channel, text, thread_ts=None):
        return app.client.chat_postMessage(channel=channel, text=text, thread_ts=thread_ts)

    def update(channel, ts, text, blocks=None):
        kwargs = {"channel": channel, "ts": ts, "text": text}
        if blocks is not None:
            kwargs["blocks"] = blocks
        return app.client.chat_update(**kwargs)

    def handle(user, channel, text, thread_ts):
        cmd = text.split()[0].lower() if text else ""

        if cmd in ("!gemini", "!codex", "!claude"):
            cli = cmd[1:]
            parts = text.split(maxsplit=2)
            # Two-arg form: !<cli> <name> <path> → dedicated channel + cwd
            if len(parts) >= 3:
                sess_name, sess_path = parts[1], parts[2]
                try:
                    ch_id, tmux_name, ch_name = start_named_session(
                        cli, sess_name, sess_path, user, app)
                except ValueError as e:
                    post(channel, f":warning: {e}", thread_ts); return
                except SlackApiError as e:
                    err = e.response.get("error", "")
                    if err == "missing_scope":
                        needed = e.response.get("needed", "groups:write")
                        post(channel,
                             f":warning: Slack app is missing scope `{needed}`. "
                             "Add it to the Slack app config and reinstall, then retry.",
                             thread_ts)
                    else:
                        post(channel, f":warning: Slack error: `{err}`", thread_ts)
                    return
                except Exception as e:
                    post(channel, f":warning: {e}", thread_ts); return
                with sessions_lock:
                    sessions[ch_id] = CLISession(
                        cli=cli, tmux_name=tmux_name, path=sess_path,
                        slack_channel_id=ch_id, is_named=True)
                post(channel,
                     f":zap: Created <#{ch_id}|{ch_name}>, invited you, and launched `{cli}` in `{sess_path}`.",
                     thread_ts)
                post(ch_id,
                     f":zap: `{cli}` ready (cwd `{sess_path}`). Send any message. `!end` to stop and archive this channel.",
                     None)
                return
            # Existing one-channel-per-DM form.
            with sessions_lock:
                if channel in sessions:
                    kill_session(sessions[channel].tmux_name)
                tmux_name = f"slack_{user}_{channel}_{cli}".replace(".", "_")
                ok = start_session(cli, tmux_name)
                if not ok:
                    post(channel, f":warning: Failed to start `{cli}` — prompt didn't appear within 30s.", thread_ts)
                    return
                sessions[channel] = CLISession(cli=cli, tmux_name=tmux_name)
            post(channel, f":zap: Started `{cli}` session. Send any message to chat. `!end` to stop, `!reset` to restart.", thread_ts)
            return

        if cmd == "!end":
            with sessions_lock:
                sess = sessions.pop(channel, None)
            if not sess:
                post(channel, "No active session.", thread_ts)
                return
            kill_session(sess.tmux_name)
            if sess.is_named and sess.slack_channel_id:
                post(sess.slack_channel_id, ":wave: Session ended. Archiving this channel.", None)
                try:
                    app.client.conversations_archive(channel=sess.slack_channel_id)
                except SlackApiError as e:
                    print(f"[archive] failed: {e.response.get('error')}")
            else:
                post(channel, ":wave: Session ended.", thread_ts)
            return

        if cmd == "!reset":
            with sessions_lock:
                sess = sessions.get(channel)
                cli = sess.cli if sess else None
                if sess:
                    kill_session(sess.tmux_name)
                    del sessions[channel]
            if not cli:
                post(channel, "No active session to reset. Try `!gemini` or `!codex`.", thread_ts)
                return
            handle(user, channel, f"!{cli}", thread_ts)
            return

        if cmd in ("!check_limit", "!limits", "!check"):
            placeholder = post(channel, ":mag: Checking AI CLI limits…", thread_ts)
            ts = placeholder["ts"]
            try:
                fallback, blocks = run_check_limit()
            except Exception as e:
                update(channel, ts, f":warning: check_limit error: `{e}`")
                return
            update(channel, ts, fallback, blocks=blocks)
            return

        if cmd == "!status":
            with sessions_lock:
                sess = sessions.get(channel)
            if sess:
                age = int(time.time() - sess.last_used)
                tag = " (named)" if sess.is_named else ""
                cwd = f", cwd `{sess.path}`" if sess.path else ""
                post(channel, f":eyes: Active `{sess.cli}` session{tag} (idle {age}s, tmux `{sess.tmux_name}`{cwd}).", thread_ts)
            else:
                post(channel, "No active session. Try `!gemini` or `!codex` first.", thread_ts)
            return

        if cmd in ("!help", "help"):
            post(channel,
                 "*Commands*\n"
                 "`!gemini` / `!codex` / `!claude` — start session in this channel\n"
                 "`!gemini <name> <path>` — create a dedicated channel + run CLI from `<path>`\n"
                 "  (also works with `!codex` / `!claude`)\n"
                 "`!check_limit` — show usage limits for all CLIs\n"
                 "`!status` — show active session\n"
                 "`!reset` — restart current session\n"
                 "`!end` — stop current session (archives the channel if it was named)\n"
                 "Anything else is forwarded to the active CLI.",
                 thread_ts)
            return

        with sessions_lock:
            sess = sessions.get(channel)
        if not sess:
            post(channel, "No active session. Start one with `!gemini`, `!codex`, or `!claude`.", thread_ts)
            return

        placeholder = post(channel, ":hourglass_flowing_sand: Thinking…", thread_ts)
        ts = placeholder["ts"]
        # Per-session lock prevents two concurrent Slack messages from typing into
        # the same tmux session in parallel and stomping each other's keystrokes.
        with sess.io_lock:
            try:
                response = send_and_wait(sess.tmux_name, text, sess.cli)
            except Exception as e:
                update(channel, ts, f":warning: Error: `{e}`")
                return
            sess.last_used = time.time()

        update(channel, ts, _format_for_slack(response))

    return handle


def _load_dotenv():
    """Load KEY=VALUE pairs from a .env file at the repo root, if present.
    Existing env vars take precedence."""
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (os.path.join(here, ".env"),
                      os.path.join(here, "..", "..", ".env")):
        path = os.path.abspath(candidate)
        if not os.path.isfile(path):
            continue
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip().strip('"').strip("'")
                os.environ.setdefault(key, val)
        return path
    return None


def main():
    _load_dotenv()
    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    app_token = os.environ.get("SLACK_APP_TOKEN")
    if not bot_token or not app_token:
        sys.exit("Set SLACK_BOT_TOKEN and SLACK_APP_TOKEN (env vars or .env file). See .env.example.")

    app = App(token=bot_token)
    handle = make_handler(app)

    @app.event("message")
    def on_message(event, logger):
        if event.get("bot_id") or event.get("subtype"):
            print(f"[message] skipped (bot_id or subtype): {event.get('subtype')}")
            return
        user = event.get("user")
        channel = event.get("channel")
        text = (event.get("text") or "").strip()
        print(f"[message] from {user} in {channel}: {text!r}")
        if not user or not channel or not text:
            return
        threading.Thread(
            target=handle,
            args=(user, channel, text, event.get("thread_ts")),
            daemon=True,
        ).start()

    @app.event("app_mention")
    def on_mention(event, logger):
        user = event.get("user")
        channel = event.get("channel")
        raw = (event.get("text") or "").strip()
        # Strip leading bot mention. Slack may use <@U123>, <@U123|name>, or
        # occasionally lowercase IDs — match permissively.
        text = re.sub(r"^<@[\w]+(\|[^>]+)?>\s*", "", raw)
        print(f"[app_mention] from {user} in {channel}: raw={raw!r} stripped={text!r}")
        if not user or not channel or not text:
            print("[app_mention] dropped: missing user/channel/text")
            return
        threading.Thread(
            target=handle,
            args=(user, channel, text, event.get("thread_ts")),
            daemon=True,
        ).start()

    # Catch-all so we see *any* event Slack sends us — useful when nothing fires.
    @app.event({"type": "message", "subtype": "message_changed"})
    def _noop_edit(event, logger):
        pass

    threading.Thread(target=cleanup_idle_loop, args=(app,), daemon=True).start()
    print("Slack bridge running. DM your bot or @mention it in a channel.")
    SocketModeHandler(app, app_token).start()


if __name__ == "__main__":
    main()
