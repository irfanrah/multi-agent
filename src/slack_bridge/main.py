"""Slack <-> CLI bridge with stateful tmux sessions.

A Slack DM (or channel where the bot is invited) becomes a persistent chat with
either Gemini CLI or Codex CLI. Each (user, channel) keeps its own tmux session
so context persists across messages.

Slack commands:
  !gemini     Start (or restart) a Gemini session
  !codex      Start (or restart) a Codex session
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
import os
import re
import sys
import time
import threading
import subprocess
from dataclasses import dataclass, field

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

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
    "gemini": {"cmd": "gemini", "ready_marker": "Type your message", "assistant_marker": "✦"},
    "codex":  {"cmd": "codex",  "ready_marker": "›",                  "assistant_marker": "•"},
}


@dataclass
class CLISession:
    cli: str
    tmux_name: str
    last_used: float = field(default_factory=time.time)


sessions: dict = {}
sessions_lock = threading.Lock()
IDLE_TIMEOUT_SEC = 30 * 60


def start_session(cli, name, max_wait=30):
    cfg = CLI_CONFIGS[cli]
    if session_exists(name):
        _tmux("kill-session", "-t", name)
    _tmux("new-session", "-d", "-s", name, "-x", "200", "-y", "50", cfg["cmd"])
    deadline = time.time() + max_wait
    while time.time() < deadline:
        if cfg["ready_marker"] in capture(name):
            return True
        time.sleep(0.5)
    return False


def kill_session(name):
    if session_exists(name):
        _tmux("kill-session", "-t", name)


CHROME_DIVIDER_RE = re.compile(
    r"^\s*\?\s+for\s+shortcuts\s*$"     # Gemini help hint
    r"|^─{3,}\s*$"                       # Gemini input separator
    r"|^▄{3,}\s*$"                       # Gemini box top
    r"|^▀{3,}\s*$"                       # Gemini box bottom
    r"|^\s*Shift\+Tab to accept edits\s*$"
    r"|^\s*›\s",                         # Codex input prompt (next placeholder)
    re.MULTILINE,
)
THINKING_RE = re.compile(r"⠋|⠙|⠹|⠸|⠼|⠴|⠦|⠧|⠇|⠏")


def _is_responding(text):
    """Heuristic: is the CLI still rendering/thinking?"""
    return ("Thinking" in text or "Working" in text or "esc to cancel" in text
            or bool(THINKING_RE.search(text)))


def send_and_wait(name, user_text, cli, max_wait=180, stable_secs=3.5, poll=1.0):
    """Send user_text + Enter, wait for the response, return cleaned text.

    These TUIs redraw the same screen region rather than scrolling, so we extract
    the response by finding the LAST assistant marker in the post-capture.
    """
    cfg = CLI_CONFIGS[cli]
    marker = cfg["assistant_marker"]

    pre = capture(name)
    pre_marker_count = pre.count(marker)

    _tmux("send-keys", "-t", name, "-l", user_text)
    time.sleep(0.4)
    _tmux("send-keys", "-t", name, "Enter")

    last, stable_at = "", None
    deadline = time.time() + max_wait
    while time.time() < deadline:
        cur = capture(name)
        # We want a NEW assistant turn (marker count increased) AND the pane
        # has been stable AND nothing is "thinking".
        new_turn = cur.count(marker) > pre_marker_count
        if new_turn and cur == last and not _is_responding(cur):
            if stable_at is None:
                stable_at = time.time()
            elif time.time() - stable_at >= stable_secs:
                break
        else:
            stable_at = None
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


def cleanup_idle_loop():
    while True:
        time.sleep(60)
        now = time.time()
        with sessions_lock:
            stale = [k for k, s in sessions.items() if now - s.last_used > IDLE_TIMEOUT_SEC]
            for k in stale:
                kill_session(sessions[k].tmux_name)
                del sessions[k]


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

    def update(channel, ts, text):
        return app.client.chat_update(channel=channel, ts=ts, text=text)

    def handle(user, channel, text, thread_ts):
        key = (user, channel)
        cmd = text.split()[0].lower() if text else ""

        if cmd in ("!gemini", "!codex"):
            cli = cmd[1:]
            with sessions_lock:
                if key in sessions:
                    kill_session(sessions[key].tmux_name)
                tmux_name = f"slack_{user}_{channel}_{cli}".replace(".", "_")
                ok = start_session(cli, tmux_name)
                if not ok:
                    post(channel, f":warning: Failed to start `{cli}` — prompt didn't appear within 30s.", thread_ts)
                    return
                sessions[key] = CLISession(cli=cli, tmux_name=tmux_name)
            post(channel, f":zap: Started `{cli}` session. Send any message to chat. `!end` to stop, `!reset` to restart.", thread_ts)
            return

        if cmd == "!end":
            with sessions_lock:
                if key in sessions:
                    kill_session(sessions[key].tmux_name)
                    del sessions[key]
                    post(channel, ":wave: Session ended.", thread_ts)
                else:
                    post(channel, "No active session.", thread_ts)
            return

        if cmd == "!reset":
            with sessions_lock:
                sess = sessions.get(key)
                cli = sess.cli if sess else None
                if sess:
                    kill_session(sess.tmux_name)
                    del sessions[key]
            if not cli:
                post(channel, "No active session to reset. Try `!gemini` or `!codex`.", thread_ts)
                return
            handle(user, channel, f"!{cli}", thread_ts)
            return

        if cmd == "!status":
            with sessions_lock:
                sess = sessions.get(key)
            if sess:
                age = int(time.time() - sess.last_used)
                post(channel, f":eyes: Active `{sess.cli}` session (idle {age}s, tmux `{sess.tmux_name}`).", thread_ts)
            else:
                post(channel, "No active session. Try `!gemini` or `!codex` first.", thread_ts)
            return

        if cmd in ("!help", "help"):
            post(channel,
                 "*Commands*\n"
                 "`!gemini` start Gemini session\n"
                 "`!codex` start Codex session\n"
                 "`!status` show active session\n"
                 "`!reset` restart current session\n"
                 "`!end` stop current session\n"
                 "Anything else is forwarded to the active CLI.",
                 thread_ts)
            return

        with sessions_lock:
            sess = sessions.get(key)
        if not sess:
            post(channel, "No active session. Start one with `!gemini` or `!codex`.", thread_ts)
            return

        placeholder = post(channel, ":hourglass_flowing_sand: Thinking…", thread_ts)
        ts = placeholder["ts"]
        try:
            response = send_and_wait(sess.tmux_name, text, sess.cli)
        except Exception as e:
            update(channel, ts, f":warning: Error: `{e}`")
            return

        with sessions_lock:
            sess.last_used = time.time()

        update(channel, ts, _format_for_slack(response))

    return handle


def main():
    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    app_token = os.environ.get("SLACK_APP_TOKEN")
    if not bot_token or not app_token:
        sys.exit("Set SLACK_BOT_TOKEN and SLACK_APP_TOKEN env vars (see module docstring).")

    app = App(token=bot_token)
    handle = make_handler(app)

    @app.event("message")
    def on_message(event, logger):
        if event.get("bot_id") or event.get("subtype"):
            return
        user = event.get("user")
        channel = event.get("channel")
        text = (event.get("text") or "").strip()
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
        text = (event.get("text") or "").strip()
        text = re.sub(r"^<@[A-Z0-9]+>\s*", "", text)
        if not user or not channel or not text:
            return
        threading.Thread(
            target=handle,
            args=(user, channel, text, event.get("thread_ts")),
            daemon=True,
        ).start()

    threading.Thread(target=cleanup_idle_loop, daemon=True).start()
    print("Slack bridge running. DM your bot or @mention it in a channel.")
    SocketModeHandler(app, app_token).start()


if __name__ == "__main__":
    main()
