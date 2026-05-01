"""Slack <-> CLI bridge with stateful tmux sessions.

A Slack DM (or channel where the bot is invited) becomes a persistent chat with
Gemini CLI, Codex CLI, or Claude Code. Each (user, channel) keeps its own tmux
session so context persists across messages.

Slack commands:
  !gemini / !codex / !claude   Start (or restart) a session
  !gemini <name> <path>        Create #<cli>-<name>-<id> agent channel + launch CLI in <path>
  !end                         End current session
  !status                      Show what's active in this channel
  !sessions / !ls              List every active bridge session globally
  !reset                       Restart in place (same cli, cwd, model)
  !cancel / !interrupt         Send Esc to interrupt the running CLI turn
  !switch <cli>                Swap the CLI in this channel, keep cwd
  !model <name>                Relaunch the active CLI with a model flag
  !run <shell-cmd>             One-shot shell exec in the session's cwd
  !upload <path>               Upload local file(s) to Slack
  !download / !dl              Save a file attached to this message into session's cwd
  !check_limit                 Usage % for Claude, Gemini, Codex with reset times
  !kill-server / !nuke         tmux kill-server: wipe ALL bridge sessions
  !help                        Print all commands
  <anything else>              Forwarded to the active CLI session

Sessions persist until !end / !reset / !kill-server (no idle timeout).

Required env:
  SLACK_BOT_TOKEN   xoxb-...   (bot user OAuth token)
  SLACK_APP_TOKEN   xapp-...   (app-level token, scope connections:write)

Required Slack scopes (Bot Token):
  Core:           app_mentions:read, chat:write, im:history, im:read, im:write
  Named sessions: groups:write, groups:read, groups:history
  File uploads:   files:write   (for !upload command)
  Branded posts:  chat:write.customize  (optional — for "CLI Bridge — Gemini" identity)

Subscribe to bot events: app_mention, message.im, message.groups
"""
import glob
import importlib.util
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
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

# Per-CLI footer noise we always want to drop from response text:
#   "✻ Brewed for 5s" — Claude's "thinking time" indicator (Brewed/Cooked/
#     Churned/Pondered/etc.)
#   "⎿  Running…" — Claude's transient tool-running line; the actual result
#     replaces it on next render, so it's pure noise by the time we capture.
#   "Press Esc to interrupt" — codex/claude transient hint
NOISE_LINE_RE = re.compile(
    r"^\s*✻\s+\w+\s+for\s+\d+s\s*$"
    r"|^\s*⎿\s+(?:Running|Streaming|Loading|Waiting)…?\s*$"
    r"|^\s*\(?(?:Press\s+)?[Ee]sc\s+to\s+(?:interrupt|cancel|exit).*$"
    r"|^\s*Press\s+enter\s+to\s+confirm\b.*$"
)
# Strip leading assistant marker so each Claude tool-call line reads naturally.
LEADING_MARKER_RE = re.compile(r"^([●•✦])\s+")


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
        # Drop CLI noise (timing hints, transient running indicators).
        if NOISE_LINE_RE.match(line):
            continue
        # Strip the leading "● " / "• " / "✦ " marker so chained tool-call lines
        # don't look like bullet points in Slack.
        line = LEADING_MARKER_RE.sub("", line, count=1)
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
    # `display_name` + `icon_emoji`: bot identity used when posting in this CLI's
    # dedicated agent channel (requires chat:write.customize scope; falls back gracefully).
    "gemini": {
        "cmd": "gemini",
        "ready_marker": "Type your message",
        "assistant_marker": "✦",
        "trust_pattern": "Do you trust the files in this folder",
        "trust_keys": "1",
        "display_name": "CLI Bridge — Gemini",
        "icon_emoji": ":sparkles:",
        "model_flag": "-m",
    },
    "codex": {
        # `-s workspace-write` sandboxes file writes to the cwd — codex can't
        # touch /etc, /usr, the home dir outside the project, etc.
        # `-a on-request` (codex's default) lets the model decide when to ask.
        # In practice it auto-approves reads (find, sort, ls, cat, …) and asks
        # before non-trivial mutations. We tried `-a untrusted` for stricter
        # safety but it asks for every read-only command outside ~10 hardcoded
        # ones — annoying. Workspace-write + on-request is the sweet spot.
        # NOTE: do NOT add --no-alt-screen — codex v0.114.0 ignores stdin
        # written by tmux send-keys when in inline mode.
        "cmd": "codex -s workspace-write -a on-request",
        "ready_marker": "›",
        "assistant_marker": "•",
        "display_name": "CLI Bridge — Codex",
        "icon_emoji": ":robot_face:",
        "model_flag": "-m",
    },
    "claude": {
        "cmd": "claude",
        "ready_marker": "? for shortcuts",
        "assistant_marker": "●",
        "trust_pattern": "Yes, I trust this folder",
        "trust_keys": "1",
        "display_name": "CLI Bridge — Claude",
        "icon_emoji": ":hatching_chick:",
        "model_flag": "--model",
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
    # Extra args appended to the CLI's launch command — currently used by !model
    # to add e.g. "-m gpt-5" or "--model opus". Preserved across !reset.
    extra_args: str = ""
    # Lock taken while a message is being typed/processed for this session.
    # Prevents two concurrent Slack messages from interleaving keystrokes.
    io_lock: threading.Lock = field(default_factory=threading.Lock)


# Keyed by Slack channel_id. DM channels are unique per user, so this also
# uniquely identifies per-user DM sessions; named sessions live in their own channel.
sessions: dict = {}
sessions_lock = threading.Lock()
# Idle reaping is disabled — sessions persist until !end / !reset / !kill-server
# (or host restart). cleanup_idle_loop is kept around for re-enable but is no
# longer started in main(); flip the constant back to e.g. 30*60 and re-add the
# Thread() spawn in main() to bring it back.
IDLE_TIMEOUT_SEC = None


def start_session(cli, name, max_wait=30, cwd=None, extra_args=""):
    cfg = CLI_CONFIGS[cli]
    if session_exists(name):
        _tmux("kill-session", "-t", name)
    args = ["new-session", "-d", "-s", name, "-x", "200", "-y", "50"]
    if cwd:
        args += ["-c", cwd]
    full_cmd = cfg["cmd"] + (" " + extra_args if extra_args else "")
    args.append(full_cmd)
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
    """Create channel `<cli>-<slug>-<uniqid>`, invite inviter, launch tmux with cwd=path.

    The uniqid suffix is always present so re-running with the same project
    name produces a fresh channel (Slack keeps archived channels around with
    their old name otherwise) and so the channel name is unique on first try.
    The tmux session uses the same name as the channel for easy lookup.

    Returns (channel_id, tmux_name, channel_name) on success.
    Raises ValueError on bad path. Raises SlackApiError or RuntimeError on Slack/CLI failures.
    """
    if not os.path.isdir(path):
        raise ValueError(f"path not found or not a directory: `{path}`")

    slug = _slugify_channel(name)
    # Channel name format: <cli>-<project>-<uniqid>, e.g. codex-cctv_date_rename-b93c.
    base_name = f"{cli}-{slug}"

    # Always append a random suffix; on the rare collision, just retry with a new one.
    channel_id = None
    final_name = None
    last_err = None
    for _ in range(3):
        try_name = f"{base_name}-{secrets.token_hex(2)}"
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

    # Set a useful channel topic — visible in Slack header.
    try:
        app.client.conversations_setTopic(
            channel=channel_id,
            topic=f"{cli} agent · cwd `{path}` · `!end` to stop")
    except SlackApiError:
        pass  # not critical

    # tmux session name == channel name. Channel names already conform to
    # [a-z0-9_-] (see _slugify_channel), which tmux accepts cleanly.
    tmux_name = final_name
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
    r"|^.*\d+%\s+left\b.*$"              # Codex footer: "gpt-X · N% left · /path"
    r"|^\s*›\s+(?!\d+\.)"                # Codex input placeholder ("› Explain this codebase")
                                          # — but NOT dialog options like "› 1. Yes"
    r"|^\s*❯\s*$",                       # Claude empty input prompt
    re.MULTILINE,
)

# A permission/tool-use dialog the user must answer. If we see one of these
# patterns BELOW the chrome divider, we keep the dialog in the response so the
# user can read it in Slack and reply with their choice.
PERMISSION_DIALOG_RE = re.compile(
    r"Do you want to proceed\?"
    r"|Would you like to run"
    r"|Allow this (?:action|command|tool)"
    r"|[›❯]\s*\d+\.\s*(?:Yes|Allow|Trust|Approve)"
    r"|\[\s*[yY]\s*/\s*[nN]\s*\]",
    re.IGNORECASE,
)
# End-of-dialog footer markers to clip at, so we don't include post-dialog chrome.
DIALOG_END_RE = re.compile(
    r"^\s*Esc to cancel\b.*$"
    r"|^\s*Press enter to confirm\b.*$",
    re.MULTILINE,
)
THINKING_RE = re.compile(r"⠋|⠙|⠹|⠸|⠼|⠴|⠦|⠧|⠇|⠏")


def _is_responding(text):
    """Heuristic: is the CLI still rendering/thinking?

    NOTE: do NOT match "esc to cancel" — codex permission dialogs include
    "Press enter to confirm or esc to cancel" in their footer, which would
    cause false positives and prevent the bridge from ever returning.
    """
    return ("Thinking" in text or "Working" in text
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
    post_marker_count = post_no_ansi.count(marker)

    # Two extraction modes:
    #   - new_turn (assistant produced a response): start from the LAST marker.
    #   - no new_turn (CLI sitting at a permission prompt with no response yet):
    #     using rfind(marker) would point at the PREVIOUS turn's response and
    #     leak it into this reply. Instead start from the user's echoed input.
    if post_marker_count > pre_marker_count:
        last_idx = post_no_ansi.rfind(marker)
        after = post_no_ansi[last_idx:]
    else:
        echo_idx = post_no_ansi.rfind(user_text)
        if echo_idx >= 0:
            eol = post_no_ansi.find("\n", echo_idx)
            after = post_no_ansi[eol + 1:] if eol >= 0 else ""
        else:
            last_idx = post_no_ansi.rfind(marker)
            after = post_no_ansi[last_idx:] if last_idx >= 0 else post_no_ansi

    # Cut off at the input chrome that sits below the conversation — UNLESS a
    # permission dialog appears below the chrome (in which case the user needs
    # to see it in Slack to answer).
    chrome = CHROME_DIVIDER_RE.search(after)
    if chrome:
        below = after[chrome.end():]
        if PERMISSION_DIALOG_RE.search(below):
            # Keep through the dialog. Trim at the dialog's "Esc to cancel" footer.
            end = DIALOG_END_RE.search(after, chrome.end())
            if end:
                after = after[:end.start()]
            # else: include everything to end of pane
        else:
            after = after[:chrome.start()]

    return clean_output(after)


def cleanup_idle_loop(app=None):
    """Periodically kill tmux sessions that have been idle past the timeout.
    For named sessions (with their own Slack channel), archive the channel too.

    NOTE: not currently spawned by main() — sessions persist indefinitely. If
    IDLE_TIMEOUT_SEC is None this loop becomes a no-op so accidentally
    re-enabling the thread doesn't immediately wipe everything.
    """
    while True:
        time.sleep(60)
        if IDLE_TIMEOUT_SEC is None:
            continue
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
    def post(channel, text, thread_ts=None, cli=None, blocks=None):
        kwargs = {"channel": channel, "text": text, "thread_ts": thread_ts}
        if blocks is not None:
            kwargs["blocks"] = blocks
        if cli and cli in CLI_CONFIGS:
            cfg = CLI_CONFIGS[cli]
            if cfg.get("display_name"):
                kwargs["username"] = cfg["display_name"]
            if cfg.get("icon_emoji"):
                kwargs["icon_emoji"] = cfg["icon_emoji"]
        try:
            return app.client.chat_postMessage(**kwargs)
        except SlackApiError as e:
            # Fall back to plain post if chat:write.customize scope is missing.
            if e.response.get("error") in ("not_allowed", "missing_scope"):
                return app.client.chat_postMessage(
                    channel=channel, text=text, thread_ts=thread_ts,
                    blocks=blocks if blocks is not None else None)
            raise

    def update(channel, ts, text, blocks=None, cli=None):
        # NOTE: chat.update doesn't accept username/icon_emoji — those are
        # post-only. Updates inherit the identity from the original post,
        # which is fine: if the placeholder was posted with the CLI's branded
        # identity, the updated text keeps that identity automatically.
        # The cli= kwarg is accepted for caller-symmetry but ignored here.
        kwargs = {"channel": channel, "ts": ts, "text": text}
        if blocks is not None:
            kwargs["blocks"] = blocks
        try:
            return app.client.chat_update(**kwargs)
        except SlackApiError as e:
            print(f"[chat_update] {e.response.get('error')}: falling back to new post")
            # If update fails for any reason, post a fresh reply so the user
            # at least sees the response.
            return app.client.chat_postMessage(channel=channel, text=text,
                                               blocks=blocks if blocks is not None else None)

    def handle(user, channel, text, thread_ts, files=None):
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
                ready_text = (
                    f":zap: `{cli}` ready in `{sess_path}`.\n"
                    "Just type your message — no `!` or `@mention` needed.\n"
                    "*Safety:* the agent runs with workspace-write permissions and "
                    "asks before risky actions (file deletes, shell commands, network "
                    "calls). When it asks, reply here in Slack with the option (`y`, "
                    "`1`, etc) — the bridge forwards your answer."
                )
                post(ch_id, ready_text, cli=cli)
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
                post(sess.slack_channel_id,
                     ":wave: Session ended. Archiving this channel.",
                     cli=sess.cli)
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
            if not sess:
                post(channel, "No active session to reset. Try `!gemini` or `!codex`.", thread_ts)
                return
            # Restart in place: same cli, cwd, tmux name, extra_args, named status.
            kill_session(sess.tmux_name)
            ok = start_session(sess.cli, sess.tmux_name, cwd=sess.path,
                               extra_args=sess.extra_args, max_wait=90)
            if not ok:
                with sessions_lock:
                    sessions.pop(channel, None)
                post(channel, f":warning: Failed to restart `{sess.cli}` (prompt didn't appear).", thread_ts)
                return
            sess.last_used = time.time()
            extra = f" with `{sess.extra_args}`" if sess.extra_args else ""
            post(channel, f":arrows_counterclockwise: Restarted `{sess.cli}`{extra}.", thread_ts)
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
                extra = f", `{sess.extra_args}`" if sess.extra_args else ""
                post(channel, f":eyes: Active `{sess.cli}` session{tag} (idle {age}s, tmux `{sess.tmux_name}`{cwd}{extra}).", thread_ts)
            else:
                post(channel, "No active session. Try `!gemini` or `!codex` first.", thread_ts)
            return

        if cmd in ("!upload", "!file", "!files"):
            # Upload local file(s) to this Slack channel. Relative paths are
            # resolved against the active session's cwd; globs are expanded.
            parts = text.split()[1:]
            if not parts:
                post(channel,
                     "Usage: `!upload <path>` (paths can be absolute, relative "
                     "to session cwd, or globs). Multiple paths allowed.",
                     thread_ts)
                return
            with sessions_lock:
                sess = sessions.get(channel)
            base = sess.path if (sess and sess.path) else os.getcwd()
            resolved = []
            for p in parts:
                # Resolve relative paths against session cwd, then glob.
                full = p if os.path.isabs(p) else os.path.join(base, p)
                matches = glob.glob(full)
                if matches:
                    resolved.extend(matches)
                else:
                    resolved.append(full)  # so we can report it as missing
            uploaded, failed = [], []
            for path in resolved[:20]:  # cap so a stray * doesn't spam Slack
                if not os.path.isfile(path):
                    failed.append(f"`{path}` (not found)")
                    continue
                try:
                    app.client.files_upload_v2(
                        channel=channel, file=path,
                        title=os.path.basename(path),
                    )
                    uploaded.append(os.path.basename(path))
                except SlackApiError as e:
                    err = e.response.get("error", "?")
                    if err == "missing_scope":
                        failed.append(
                            f"`{os.path.basename(path)}` — bot needs `files:write` "
                            "scope; add it in OAuth & Permissions and reinstall.")
                        break  # no point retrying others with same error
                    failed.append(f"`{os.path.basename(path)}` ({err})")
            lines = []
            if uploaded:
                lines.append(f":outbox_tray: Uploaded {len(uploaded)} file(s).")
            if failed:
                lines.append("Issues:\n  • " + "\n  • ".join(failed))
            if not lines:
                lines.append("Nothing to upload.")
            post(channel, "\n".join(lines), thread_ts)
            return

        if cmd in ("!kill-server", "!killserver", "!nuke"):
            # Wipe everything: kill the entire tmux server (all bridge sessions),
            # clear our sessions dict, archive any orphan named channels.
            with sessions_lock:
                snapshot = list(sessions.items())
                sessions.clear()
            try:
                subprocess.run(["tmux", "kill-server"], stderr=subprocess.DEVNULL)
            except Exception as e:
                post(channel, f":warning: tmux kill-server failed: `{e}`", thread_ts)
                return
            archived = 0
            for ch_id, sess in snapshot:
                if sess.is_named and sess.slack_channel_id:
                    try:
                        app.client.conversations_archive(channel=sess.slack_channel_id)
                        archived += 1
                    except SlackApiError:
                        pass
            post(channel,
                 f":bomb: tmux kill-server done. "
                 f"Killed {len(snapshot)} session(s); archived {archived} agent channel(s).",
                 thread_ts)
            return

        if cmd == "!run":
            # One-shot shell command in the session's cwd. No agent involved.
            rest = text[len(cmd):].strip()
            if not rest:
                post(channel, "Usage: `!run <shell command>` (runs in this session's cwd, 30s timeout).", thread_ts)
                return
            with sessions_lock:
                sess = sessions.get(channel)
            cwd = sess.path if (sess and sess.path) else os.getcwd()
            try:
                proc = subprocess.run(
                    rest, shell=True, cwd=cwd,
                    capture_output=True, text=True, timeout=30,
                )
            except subprocess.TimeoutExpired:
                post(channel, ":alarm_clock: Command timed out after 30s.", thread_ts)
                return
            except Exception as e:
                post(channel, f":warning: `!run` error: `{e}`", thread_ts)
                return
            CAP = 6000
            parts = []
            if proc.stdout.strip():
                out = proc.stdout
                trunc = "" if len(out) <= CAP else f"\n... (truncated, {len(out) - CAP} more bytes)"
                parts.append(f"*stdout:*\n```\n{out[:CAP]}{trunc}\n```")
            if proc.stderr.strip():
                err = proc.stderr
                trunc = "" if len(err) <= CAP else f"\n... (truncated, {len(err) - CAP} more bytes)"
                parts.append(f"*stderr:*\n```\n{err[:CAP]}{trunc}\n```")
            if proc.returncode != 0:
                parts.append(f"_exit {proc.returncode}_")
            if not parts:
                parts.append(f"_(exit {proc.returncode}, no output)_")
            post(channel, "\n".join(parts), thread_ts)
            return

        if cmd in ("!download", "!dl"):
            # Pull files attached to the same Slack message into the session's cwd.
            if not files:
                post(channel,
                     "Attach a file to the same message and include `!download` "
                     "(or `!dl`) in the text.",
                     thread_ts)
                return
            with sessions_lock:
                sess = sessions.get(channel)
            base = sess.path if (sess and sess.path) else os.getcwd()
            saved, failed = [], []
            bot_token = app.client.token
            for f in (files or [])[:10]:
                url = f.get("url_private_download") or f.get("url_private")
                name = f.get("name") or f.get("id") or "download"
                if not url:
                    failed.append(f"`{name}` (no URL)")
                    continue
                # Strip path components — never let a Slack-controlled name
                # write outside the session cwd.
                safe = os.path.basename(name) or "download"
                dest = os.path.join(base, safe)
                try:
                    req = urllib.request.Request(
                        url, headers={"Authorization": f"Bearer {bot_token}"},
                    )
                    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as out:
                        shutil.copyfileobj(r, out)
                    saved.append(safe)
                except Exception as e:
                    failed.append(f"`{safe}` ({e})")
            lines = []
            if saved:
                lines.append(
                    f":inbox_tray: Saved {len(saved)} file(s) to `{base}`: "
                    + ", ".join(f"`{n}`" for n in saved))
            if failed:
                lines.append("Issues:\n  • " + "\n  • ".join(failed))
            if not lines:
                lines.append("Nothing downloaded.")
            post(channel, "\n".join(lines), thread_ts)
            return

        if cmd in ("!cancel", "!interrupt", "!stop"):
            with sessions_lock:
                sess = sessions.get(channel)
            if not sess:
                post(channel, "No active session.", thread_ts)
                return
            # Esc is the documented interrupt key for all three CLIs ("Press Esc
            # to interrupt"). We deliberately do NOT send Ctrl-C — that risks
            # killing the CLI process entirely.
            _tmux("send-keys", "-t", sess.tmux_name, "Escape")
            sess.last_used = time.time()
            post(channel, f":octagonal_sign: Sent interrupt to `{sess.cli}`.", thread_ts)
            return

        if cmd == "!switch":
            parts = text.split(maxsplit=1)
            valid = "/".join(f"`{c}`" for c in CLI_CONFIGS)
            if len(parts) < 2 or parts[1].strip().lower() not in CLI_CONFIGS:
                post(channel, f"Usage: `!switch <{'|'.join(CLI_CONFIGS)}>`. Valid: {valid}.", thread_ts)
                return
            new_cli = parts[1].strip().lower()
            with sessions_lock:
                sess = sessions.get(channel)
            if not sess:
                post(channel, "No active session. Start one first with `!gemini`/`!codex`/`!claude`.", thread_ts)
                return
            if new_cli == sess.cli:
                post(channel, f"Already running `{new_cli}`.", thread_ts)
                return
            kill_session(sess.tmux_name)
            ok = start_session(new_cli, sess.tmux_name, cwd=sess.path, max_wait=90)
            if not ok:
                with sessions_lock:
                    sessions.pop(channel, None)
                post(channel,
                     f":warning: Failed to start `{new_cli}` — prompt didn't appear within 90s. "
                     "Session removed; restart with `!{new_cli}`.",
                     thread_ts)
                return
            with sessions_lock:
                sess.cli = new_cli
                sess.extra_args = ""  # different CLI, model flags don't carry over
                sess.last_used = time.time()
            cwd_note = f" in `{sess.path}`" if sess.path else ""
            post(channel,
                 f":arrows_counterclockwise: Switched to `{new_cli}`{cwd_note}.",
                 thread_ts)
            return

        if cmd == "!model":
            parts = text.split(maxsplit=1)
            with sessions_lock:
                sess = sessions.get(channel)
            if not sess:
                post(channel, "No active session.", thread_ts)
                return
            cfg = CLI_CONFIGS[sess.cli]
            flag = cfg.get("model_flag")
            if not flag:
                post(channel, f":warning: `{sess.cli}` does not support `!model`.", thread_ts)
                return
            if len(parts) < 2 or not parts[1].strip():
                cur = f"`{sess.extra_args}`" if sess.extra_args else "(default)"
                post(channel,
                     f"Current `{sess.cli}` extra args: {cur}. Usage: `!model <name>` "
                     f"(e.g. `!model opus`, `!model gpt-5`, `!model gemini-2.5-flash`).",
                     thread_ts)
                return
            model = parts[1].strip()
            extra = f"{flag} {shlex.quote(model)}"
            kill_session(sess.tmux_name)
            ok = start_session(sess.cli, sess.tmux_name, cwd=sess.path,
                               extra_args=extra, max_wait=90)
            if not ok:
                with sessions_lock:
                    sessions.pop(channel, None)
                post(channel,
                     f":warning: Failed to relaunch `{sess.cli}` with `{extra}`. "
                     "The model name may be invalid; session removed.",
                     thread_ts)
                return
            with sessions_lock:
                sess.extra_args = extra
                sess.last_used = time.time()
            post(channel,
                 f":arrows_counterclockwise: Restarted `{sess.cli}` with `{extra}`.",
                 thread_ts)
            return

        if cmd in ("!sessions", "!ls", "!list"):
            now = time.time()
            with sessions_lock:
                snapshot = list(sessions.items())
            if not snapshot:
                post(channel, "No active sessions.", thread_ts)
                return
            lines = [f"*{len(snapshot)} active session(s):*"]
            for ch_id, s in snapshot:
                age = int(now - s.last_used)
                ch_link = f"<#{ch_id}>" if s.is_named else f"`{ch_id}`"
                cwd = f", cwd `{s.path}`" if s.path else ""
                extra = f", `{s.extra_args}`" if s.extra_args else ""
                named = " (named)" if s.is_named else ""
                lines.append(
                    f"  • {ch_link} — `{s.cli}`{named}, idle {age}s, "
                    f"tmux `{s.tmux_name}`{cwd}{extra}")
            post(channel, "\n".join(lines), thread_ts)
            return

        if cmd in ("!help", "help"):
            post(channel,
                 "*CLI Bridge — commands*\n"
                 "\n"
                 "*Start a session*\n"
                 "`!gemini`  /  `!codex`  /  `!claude`\n"
                 "    Start a session in *this* channel (DM is best).\n"
                 "`!gemini <name> <path>`  (also works with `!codex` / `!claude`)\n"
                 "    Create a dedicated channel `#<cli>-<name>-<uniqid>` "
                 "(e.g. `#codex-cctv_date_rename-b93c`), invite you, "
                 "launch the CLI with `cwd=<path>`. Each project gets its own channel.\n"
                 "\n"
                 "*Inside an agent channel*\n"
                 "Type any plain message — no `@mention`, no `!`. The bridge forwards "
                 "your text to the running CLI and posts back its reply.\n"
                 "When the agent asks for permission (e.g. `Allow rm -rf? [y/n]`), "
                 "answer right here in Slack — the bridge forwards your answer.\n"
                 "\n"
                 "*Session control*\n"
                 "`!status` — what's running in this channel\n"
                 "`!reset`  — kill the CLI and relaunch with the same config (cli, cwd, model)\n"
                 "`!cancel` (alias `!interrupt`, `!stop`) — send Esc to the active CLI to interrupt mid-turn\n"
                 "`!switch <gemini|codex|claude>` — swap the CLI in this channel, keep the cwd\n"
                 "`!model <name>` — relaunch the active CLI with a model flag "
                 "(e.g. `!model opus`, `!model gpt-5`, `!model gemini-2.5-flash`)\n"
                 "`!end`    — stop the session (named channels are archived)\n"
                 "\n"
                 "*Files & shell*\n"
                 "`!run <cmd>` — run a shell command in the session's cwd (30s timeout). "
                 "Token-free way to peek at state (`!run git status`, `!run ls`).\n"
                 "`!upload <path>` — upload local file(s) to this channel. "
                 "Paths can be absolute, relative to session cwd, or globs (e.g. "
                 "`!upload assets/*.png`).\n"
                 "`!download` (alias `!dl`) — attach a file to your message + include "
                 "`!download` to save it into the session's cwd. Useful for sharing "
                 "screenshots or PDFs with the agent.\n"
                 "\n"
                 "*Other*\n"
                 "`!sessions` (alias `!ls`) — list every active bridge session globally\n"
                 "`!check_limit` — usage % for Claude, Gemini, Codex with reset times\n"
                 "`!kill-server` — `tmux kill-server`: wipe ALL bridge sessions and "
                 "archive their channels. Use when sessions are stuck or you want a "
                 "clean slate.\n"
                 "`!help` — this message\n"
                 "\n"
                 "*Safety:* agents run with workspace-write sandboxes and *ask before* "
                 "risky actions (deletes, shell, network). The bridge can't auto-answer "
                 "those — you do, in Slack. `!run` is local shell; mind what you type.\n"
                 "*Lifetime:* sessions persist until you `!end` / `!reset` / `!kill-server` "
                 "(no idle timeout).",
                 thread_ts)
            return

        with sessions_lock:
            sess = sessions.get(channel)
        if not sess:
            post(channel, "No active session. Start one with `!gemini`, `!codex`, or `!claude`.", thread_ts)
            return

        # IMPORTANT: post placeholder WITHOUT custom username/icon. Slack's
        # chat.update rejects updates on messages with overridden identity, which
        # would leave the placeholder permanently stuck on "Thinking…" while the
        # real response gets posted as a separate message.
        placeholder = post(channel, ":hourglass_flowing_sand: Thinking…",
                           thread_ts=thread_ts)
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
        if event.get("bot_id"):
            return
        # Allow file_share subtype through — file uploads land as messages with
        # subtype="file_share" (legacy) or no subtype but with a `files` array
        # (modern). Skip other subtypes (channel joins, edits, etc).
        subtype = event.get("subtype")
        files = event.get("files") or []
        if subtype and not files:
            print(f"[message] skipped subtype={subtype}")
            return
        user = event.get("user")
        channel = event.get("channel")
        text = (event.get("text") or "").strip()
        print(f"[message] from {user} in {channel}: {text!r} files={len(files)}")
        if not user or not channel or (not text and not files):
            return
        threading.Thread(
            target=handle,
            args=(user, channel, text, event.get("thread_ts"), files),
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
        files = event.get("files") or []
        print(f"[app_mention] from {user} in {channel}: raw={raw!r} stripped={text!r}")
        if not user or not channel or (not text and not files):
            print("[app_mention] dropped: missing user/channel/text")
            return
        threading.Thread(
            target=handle,
            args=(user, channel, text, event.get("thread_ts"), files),
            daemon=True,
        ).start()

    # Catch-all so we see *any* event Slack sends us — useful when nothing fires.
    @app.event({"type": "message", "subtype": "message_changed"})
    def _noop_edit(event, logger):
        pass

    # Idle-reaping is disabled by design — sessions persist until !end / !reset
    # / !kill-server (or host restart). To re-enable, uncomment the line below
    # and set IDLE_TIMEOUT_SEC at the top of the file to e.g. 30*60.
    # threading.Thread(target=cleanup_idle_loop, args=(app,), daemon=True).start()
    print("Slack bridge running. DM your bot or @mention it in a channel.")
    SocketModeHandler(app, app_token).start()


if __name__ == "__main__":
    main()
