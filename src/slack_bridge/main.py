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
import fnmatch
import glob
import importlib.util
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from typing import Optional

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.errors import SlackApiError


def _load_dotenv():
    """Load KEY=VALUE pairs from a .env file at the repo root, if present.
    Existing env vars take precedence (`os.environ.setdefault`).

    Defined and called at module load time so module-level constants
    (e.g. LINK_UPLOAD_PASSWORD) can read their `.env` overrides before
    the rest of the bridge boots.
    """
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


_load_dotenv()  # populate os.environ from .env now, before constants below


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
BOX_RE = re.compile(r"[│╭╯╰╮─█░▝▘▛▜▟▞▖▗▎▏▬▀▄▔▁┌┐└┘├┤┬┴┼═║╔╗╚╝╠╣╦╩╬]")
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


# ---- folder zip (used by !upload) -------------------------------------------

# Skip the usual project junk so a zip of "the project folder" doesn't ship
# the user's local git history, virtualenvs, or pycache. Excluding by default
# is opinionated; if a user wanted the .git they can zip externally.
ZIP_EXCLUDED_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache"}
ZIP_EXCLUDED_PATTERNS = ("*.pyc", "*.pyo", ".DS_Store")
# Pre-check size cap for Slack-direct uploads: matches Slack's bot upload
# limit. Anything bigger should go via the pixeldrain link path
# (`!upload --link` or option `2` in the menu), which doesn't hit this cap.
ZIP_MAX_BYTES = 1024 * 1024 * 1024


def _zip_should_skip_file(name):
    return any(fnmatch.fnmatch(name, p) for p in ZIP_EXCLUDED_PATTERNS)


def _walk_for_zip(src_dir):
    """Yield (full_path, arcname) for files we'd include in the zip.

    `arcname` is relative to a top-level dir matching the src_dir basename, so
    a recipient extracting `myproj.zip` gets a single `myproj/` folder.
    """
    src = os.path.abspath(src_dir)
    arc_root = os.path.basename(src) or "archive"
    for root, dirs, files in os.walk(src):
        # Mutate dirs in place so os.walk skips junk subtrees entirely.
        dirs[:] = [d for d in dirs if d not in ZIP_EXCLUDED_DIRS]
        for fname in files:
            if _zip_should_skip_file(fname):
                continue
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, src)
            yield full, os.path.join(arc_root, rel)


def directory_size_for_zip(src_dir):
    """Sum file sizes for what _walk_for_zip would include. Cheap pre-check."""
    total = 0
    for full, _arc in _walk_for_zip(src_dir):
        try:
            total += os.path.getsize(full)
        except OSError:
            pass
    return total


def zip_directory(src_dir, dest_zip):
    """Write a zip of src_dir to dest_zip with deflate compression.

    Files matching ZIP_EXCLUDED_DIRS / ZIP_EXCLUDED_PATTERNS are skipped.
    Returns the number of files written.
    """
    count = 0
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for full, arc in _walk_for_zip(src_dir):
            try:
                zf.write(full, arcname=arc)
                count += 1
            except OSError:
                # Symlink loops, permission errors, etc — skip but don't abort
                # the whole upload.
                continue
    return count


# ---- password-protected zip + pixeldrain upload (used by !upload menu) -----

# Always-on password for link-host-bound uploads. Encrypts with classic
# ZipCrypto (Info-ZIP `-e`); not strong against a determined attacker but
# enough to gate casual access on top of the link's expiry.
# Password applied to every encrypted zip uploaded via the pixeldrain
# (option `2`) flow. Override per-deployment with the `LINK_UPLOAD_PASSWORD`
# env var; the default exists only so the bridge runs out-of-the-box. Once
# you publish anything via this path, anyone with the URL + this password
# can extract — set your own value if that matters.
LINK_UPLOAD_PASSWORD = os.environ.get("LINK_UPLOAD_PASSWORD") or "changeme"
# pixeldrain is the primary public-link host. We previously used transfer.sh
# but it was unreachable from a user's network — pixeldrain has a similar
# PUT-style API (`PUT /api/file/<name>` returning JSON `{"id": "..."}`),
# higher size cap (20 GB), and longer retention (anonymous files keep for
# ~60 days from last view at the time of writing).
PIXELDRAIN_ENDPOINT = "https://pixeldrain.com"
LINK_UPLOAD_TIMEOUT_SEC = 600  # 10 min — uploads of a few hundred MB take time

# Set by `main()` once via auth_test; readable by handlers (e.g. !debug,
# on_message). Stays None if auth_test failed at startup.
BRIDGE_BOT_USER_ID = None


def zip_paths_encrypted(sources, dest_zip, password, base_cwd=None):
    """Zip the given relative source paths into dest_zip with `zip -e -P`.

    `sources` is a list of paths *relative to* base_cwd; they are passed to
    `zip` via stdin (`-@`) so we don't blow up the argv length on big trees.
    Uses the system `zip` binary because Python stdlib zipfile can't write
    encrypted archives.
    """
    if not sources:
        raise ValueError("no sources to zip")
    cwd = base_cwd or os.getcwd()
    proc = subprocess.run(
        ["zip", "-q", "-e", "-P", password, "-@", dest_zip],
        input="\n".join(sources),
        cwd=cwd, text=True, capture_output=True, timeout=600,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"zip failed (rc={proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
        )


def make_encrypted_zip_for_upload(source_path, dest_zip, password):
    """Build a password-protected zip for a file or directory.

    - Folder: includes its tree under <basename>/, skipping ZIP_EXCLUDED_*.
    - File:   wraps the single file at its basename inside the zip.

    Returns the number of source files included.
    """
    src = os.path.abspath(source_path)
    if os.path.isdir(src):
        rels = []
        for full, _arc in _walk_for_zip(src):
            rels.append(os.path.relpath(full, os.path.dirname(src)))
        zip_paths_encrypted(rels, dest_zip, password,
                            base_cwd=os.path.dirname(src))
        return len(rels)
    if os.path.isfile(src):
        zip_paths_encrypted([os.path.basename(src)], dest_zip, password,
                            base_cwd=os.path.dirname(src))
        return 1
    raise FileNotFoundError(src)


def _build_multipart(fields, file_field, filename, file_bytes,
                     file_content_type="application/zip"):
    """Construct a multipart/form-data body. Returns (content_type, body).
    `fields` = ordered list of (name, value) string pairs (sent before file).
    """
    boundary = "----LINKUPLOADBOUNDARY" + secrets.token_hex(8)
    parts = []
    for k, v in fields:
        parts.append(
            (f"--{boundary}\r\n"
             f'Content-Disposition: form-data; name="{k}"\r\n\r\n'
             f"{v}\r\n").encode())
    parts.append(
        (f"--{boundary}\r\n"
         f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
         f"Content-Type: {file_content_type}\r\n\r\n").encode())
    parts.append(file_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return (f"multipart/form-data; boundary={boundary}", b"".join(parts))


def _upload_pixeldrain_post(file_path, *, timeout):
    """POST multipart to pixeldrain. Returns viewer URL on success.

    The PUT-style endpoint (`PUT /api/file/<name>`) hangs from some
    networks; the multipart POST works in those same environments.
    """
    import json
    name = os.path.basename(file_path)
    with open(file_path, "rb") as f:
        body_bytes = f.read()
    ctype, body = _build_multipart([], "file", name, body_bytes)
    req = urllib.request.Request(
        f"{PIXELDRAIN_ENDPOINT}/api/file",
        data=body, method="POST",
        headers={"Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace").strip()
    try:
        data = json.loads(raw)
    except ValueError:
        raise RuntimeError(f"pixeldrain (POST) non-JSON: {raw[:200]}")
    file_id = data.get("id") or data.get("file_id")
    if not file_id:
        raise RuntimeError(f"pixeldrain (POST) JSON missing id: {data}")
    return f"{PIXELDRAIN_ENDPOINT}/u/{file_id}"


def _upload_pixeldrain_put(file_path, *, timeout):
    """PUT raw body to pixeldrain. Returns viewer URL on success."""
    import json
    name = os.path.basename(file_path)
    url = f"{PIXELDRAIN_ENDPOINT}/api/file/{urllib.parse.quote(name)}"
    with open(file_path, "rb") as f:
        body = f.read()
    req = urllib.request.Request(url, data=body, method="PUT",
                                 headers={"Content-Type": "application/zip"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace").strip()
    try:
        data = json.loads(raw)
    except ValueError:
        raise RuntimeError(f"pixeldrain (PUT) non-JSON: {raw[:200]}")
    file_id = data.get("id") or data.get("file_id")
    if not file_id:
        raise RuntimeError(f"pixeldrain (PUT) JSON missing id: {data}")
    return f"{PIXELDRAIN_ENDPOINT}/u/{file_id}"


def _upload_catbox(file_path, *, timeout):
    """POST multipart to catbox.moe. Returns plain URL string.

    Permanent retention (no auto-expiry), 200 MB hard cap. Anonymous
    `reqtype=fileupload`. Simplest API of the lot.
    """
    name = os.path.basename(file_path)
    with open(file_path, "rb") as f:
        body_bytes = f.read()
    ctype, body = _build_multipart(
        [("reqtype", "fileupload")],
        "fileToUpload", name, body_bytes)
    req = urllib.request.Request(
        "https://catbox.moe/user/api.php",
        data=body, method="POST",
        headers={"Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        link = resp.read().decode("utf-8", errors="replace").strip()
    if not link.startswith("https://"):
        raise RuntimeError(f"catbox returned non-URL: {link[:200]}")
    return link


def _upload_0x0(file_path, *, timeout):
    """POST multipart to 0x0.st. Returns plain URL. ~512 MiB cap.

    0x0.st applies a User-Agent restriction — they reject anonymous-looking
    UAs to prevent abuse, so we identify as our app explicitly.
    """
    name = os.path.basename(file_path)
    with open(file_path, "rb") as f:
        body_bytes = f.read()
    ctype, body = _build_multipart([], "file", name, body_bytes)
    req = urllib.request.Request(
        "https://0x0.st",
        data=body, method="POST",
        headers={"Content-Type": ctype, "User-Agent": "slack-bridge/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        link = resp.read().decode("utf-8", errors="replace").strip()
    if not link.startswith("http"):
        raise RuntimeError(f"0x0.st returned non-URL: {link[:200]}")
    return link


# Ordered fallback chain for the !upload --link path.
#   pixeldrain-post — works where PUT hangs; biggest size cap (20 GB);
#                     ~60d retention; viewer URL.
#   catbox          — simplest API, permanent retention, 200 MB cap.
#   0x0             — multipart POST, plain URL, ~512 MiB cap, retention by size.
#   pixeldrain-put  — original API, kept as last resort.
# Override default order with the `LINK_UPLOAD_HOSTS` env var
# (comma-separated host names from the dict below).
_LINK_UPLOADERS = {
    "pixeldrain-post": _upload_pixeldrain_post,
    "pixeldrain-put":  _upload_pixeldrain_put,
    "catbox":          _upload_catbox,
    "0x0":             _upload_0x0,
}
DEFAULT_UPLOADER_ORDER = ["pixeldrain-post", "catbox", "0x0", "pixeldrain-put"]
PER_HOST_TIMEOUT_SEC = 90  # quickly fall through when one hangs


def link_upload(file_path):
    """Try the configured fallback chain. Returns (host_name, public_url).
    Raises RuntimeError if every host fails or times out.
    """
    env_order = os.environ.get("LINK_UPLOAD_HOSTS", "").strip()
    order = ([h.strip() for h in env_order.split(",") if h.strip()]
             if env_order else list(DEFAULT_UPLOADER_ORDER))
    errors = []
    for host_name in order:
        fn = _LINK_UPLOADERS.get(host_name)
        if not fn:
            errors.append(f"{host_name}: unknown host")
            continue
        try:
            print(f"[link-upload] trying {host_name}…")
            link = fn(file_path, timeout=PER_HOST_TIMEOUT_SEC)
            print(f"[link-upload] {host_name} ok → {link}")
            return host_name, link
        except Exception as e:
            print(f"[link-upload] {host_name} failed: {e}")
            errors.append(f"{host_name}: {e}")
    raise RuntimeError("all link-upload hosts failed: " + "; ".join(errors))


# Compatibility shim: existing call sites used `pixeldrain_put` directly;
# point that name at the new fallback chain so callers get retries
# automatically. Tests mock this name.
def pixeldrain_put(file_path, *, endpoint=None, timeout=None):  # noqa: ARG001 (compat sig)
    """Compatibility wrapper — runs the full fallback chain via
    `link_upload()` and returns just the URL (tests still expect a
    single URL string)."""
    _name, url = link_upload(file_path)
    return url


# ---- pending uploads (powers the !upload 1/2 menu) --------------------------

# Per-channel "I just typed !upload, waiting for you to choose 1 or 2" state.
# A bare `1` / `2` reply only counts as a menu pick when the channel has a
# fresh pending entry — otherwise it falls through to the active CLI session
# (so codex/claude permission dialogs still work).
pending_uploads: dict = {}
pending_uploads_lock = threading.Lock()
PENDING_UPLOAD_TTL_SEC = 120


def _gc_pending_uploads(now=None):
    now = now if now is not None else time.time()
    with pending_uploads_lock:
        expired = [k for k, v in pending_uploads.items() if v["expires_at"] < now]
        for k in expired:
            pending_uploads.pop(k, None)


def _take_pending_upload(channel):
    """Pop and return the pending upload for `channel`, or None if none/expired."""
    _gc_pending_uploads()
    with pending_uploads_lock:
        return pending_uploads.pop(channel, None)


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


def pane_tail(name, n=60):
    """Last `n` lines of the cleaned pane.

    Used by !raw and as a fallback when send_and_wait's chrome-clipping
    leaves the response empty (e.g. monitor/streaming commands where the
    input prompt re-renders below the output and the chrome regex eats
    everything below it). No chrome clipping here — only the line-level
    `clean_output` (ANSI/box/spinner/noise stripping).
    """
    cleaned = clean_output(capture(name))
    lines = cleaned.split("\n")
    if n > 0 and len(lines) > n:
        cleaned = "\n".join(lines[-n:])
    return cleaned


def pane_tail_after_user_input(name, user_text, max_lines=20):
    """Cleaned pane content that appears AFTER the user's last echoed input.

    When send_and_wait extracts nothing, the bottom-N-lines fallback
    (`pane_tail`) often shows stale content because most of the new turn is
    tool-call chrome that `clean_output` strips, leaving only an older
    response near the end of the buffer. Anchoring at the user's echoed
    input means we only show *recent* activity.

    Also strips chrome lines (TUI footer, prompt placeholders, divider runs)
    via CHROME_DIVIDER_RE so we don't dump the gemini/codex frame back to
    Slack. Returns "" when nothing meaningful is below the user echo
    (caller can switch to a "still working" message).
    """
    raw = capture(name)
    raw_no_ansi = ANSI_RE.sub("", raw)
    if not user_text:
        return ""
    idx = raw_no_ansi.rfind(user_text)
    if idx < 0:
        return ""
    eol = raw_no_ansi.find("\n", idx)
    after = raw_no_ansi[eol + 1:] if eol >= 0 else ""
    cleaned = clean_output(after)
    out_lines = []
    for ln in cleaned.split("\n"):
        if not ln.strip():
            continue
        if CHROME_DIVIDER_RE.match(ln):
            continue
        out_lines.append(ln)
    if max_lines > 0 and len(out_lines) > max_lines:
        out_lines = out_lines[-max_lines:]
    return "\n".join(out_lines)


def extract_pending_dialog(name, scan_lines=80):
    """Return the most recent permission dialog block on the pane, or "".

    Scans the last `scan_lines` of the cleaned pane for PERMISSION_DIALOG_RE.
    If found, returns from the dialog's question line through the dialog
    footer (DIALOG_END_RE: "Esc to cancel" / "Press enter to confirm" / blank
    run after the options). Use this BEFORE the bottom-tail fallback so a
    waiting permission dialog is the actionable message we surface to Slack.
    """
    cleaned = clean_output(capture(name))
    lines = cleaned.split("\n")
    if scan_lines > 0 and len(lines) > scan_lines:
        lines = lines[-scan_lines:]
    # Find the LAST dialog (most recent — earlier ones may already be answered).
    last_match_line = -1
    for i, ln in enumerate(lines):
        if PERMISSION_DIALOG_RE.search(ln):
            last_match_line = i
    if last_match_line < 0:
        return ""
    # Walk backwards up to N lines to find the question line. Pattern:
    #   "  Apply this change?"           <- question
    #   ""                               <- blank (don't stop here)
    #   "  ● 1. Allow once"              <- regex matched here
    # Skip blank lines while searching; only anchor on a "?" or a known
    # dialog header.
    start = last_match_line
    back_max = 10
    for back in range(1, back_max + 1):
        j = last_match_line - back
        if j < 0:
            break
        ln = lines[j]
        if "?" in ln or any(w in ln for w in (
            "Apply this change", "Would you like", "Allow execution",
            "Do you want", "Allow this",
        )):
            start = j
            break
    # Walk forwards to the dialog end (footer or blank line after options).
    end = last_match_line
    for k in range(last_match_line + 1, len(lines)):
        ln = lines[k]
        if DIALOG_END_RE.match(ln) or not ln.strip():
            break
        end = k
    block = "\n".join(lines[start:end + 1])
    return block.strip()


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
        # `-a untrusted` is a deliberate SAFETY choice: codex auto-approves only
        # a small allowlist of read-only commands (find, sort, ls, cat, …) and
        # asks for everything else, including any model-decided `rm` / file
        # write / shell mutation. The previous setting `-a on-request` let the
        # model decide when to ask; in practice the model treated explicit user
        # phrases like "delete X" as authorization and ran without a dialog,
        # which broke the bridge's promise that destructive actions surface in
        # Slack. The friction (more prompts) is the intended cost.
        # NOTE: do NOT add --no-alt-screen — codex v0.114.0 ignores stdin
        # written by tmux send-keys when in inline mode.
        "cmd": "codex -s workspace-write -a untrusted",
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
    # Register the CLISession in the global dict BEFORE the long tmux startup
    # blocks. Without this, a `!sessions` typed in the agent channel during
    # the 30–90s gemini/codex boot returns "No active sessions" because the
    # record hasn't been added yet. On startup failure we pop it back out.
    with sessions_lock:
        sessions[channel_id] = CLISession(
            cli=cli, tmux_name=tmux_name, path=path,
            slack_channel_id=channel_id, is_named=True,
        )
    # 90s instead of 30s — snap apps (gemini, codex) can be slow to start under
    # memory/CPU pressure. If we timeout, capture the pane to help diagnose.
    if not start_session(cli, tmux_name, cwd=path, max_wait=90):
        try:
            tail = capture(tmux_name)[-600:] or "(empty)"
        except Exception:
            tail = "(capture failed)"
        kill_session(tmux_name)
        with sessions_lock:
            sessions.pop(channel_id, None)
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


# Channel-name pattern for an auto-created agent channel:
# <cli>-<slug>-<4-hex-uniqid>. We use this to recognize that a previously-
# created channel can be auto-relinked to a still-live tmux session even if
# the bridge's in-memory `sessions` dict was wiped (e.g. on bridge restart
# or socket race).
NAMED_CHANNEL_RE = re.compile(
    r"^(?P<cli>" + "|".join(re.escape(c) for c in CLI_CONFIGS) + r")"
    r"-(?P<slug>.+)-(?P<uniqid>[0-9a-f]{4})$"
)


def try_relink_session(channel, app):
    """If `channel` is an agent channel whose tmux session is still alive,
    rebuild the in-memory CLISession and return it. Returns None when the
    channel doesn't match the agent-channel pattern, can't be looked up,
    or has no live tmux session.

    This is the bridge's recovery path: the tmux session is the source of
    truth; if it exists, the bridge can pick up where the previous bridge
    process left off (modulo `path`/`extra_args` which we can't recover —
    they affect `!run` cwd default and `!reset` flag preservation, both
    minor).
    """
    try:
        info = app.client.conversations_info(channel=channel)
    except Exception as e:
        print(f"[relink] conversations_info({channel}) failed: {e}")
        return None
    name = info.get("channel", {}).get("name", "")
    m = NAMED_CHANNEL_RE.match(name)
    if not m:
        return None
    cli = m.group("cli")
    if not session_exists(name):
        # Channel name matches the pattern but no live tmux pane — can't
        # recover. The user has to !<cli> <name> <path> to start fresh.
        print(f"[relink] {channel} name={name!r} matches pattern but tmux "
              f"session is gone; cannot recover")
        return None
    sess = CLISession(
        cli=cli, tmux_name=name, path=None,
        slack_channel_id=channel, is_named=True,
    )
    with sessions_lock:
        # Race: another thread might have inserted in the meantime.
        existing = sessions.get(channel)
        if existing is not None:
            return existing
        sessions[channel] = sess
    print(f"[relink] {channel} → {name!r} ({cli}); recovered from existing tmux")
    return sess


def kill_session(name):
    if session_exists(name):
        _tmux("kill-session", "-t", name)


CHROME_DIVIDER_RE = re.compile(
    r"^.*\?\s+for\s+shortcuts.*$"        # Gemini/Claude help hint (may have trailing status)
    r"|^─{3,}.*$"                        # Horizontal divider (Gemini, Claude)
    r"|^▄{3,}\s*$"                       # Gemini box top
    r"|^▀{3,}\s*$"                       # Gemini box bottom
    r"|^\s*Shift\+Tab to accept edits\s*$"
    r"|^\s*shell mode enabled.*$"        # Gemini shell-mode banner
    r"|^\s*workspace\s+\(/directory\).*$"  # Gemini footer header row
    r"|^\s*[!>]\s+Type your\s+(?:shell command|message).*$"  # Gemini input placeholders
    r"|^.*\d+%\s+left\b.*$"              # Codex footer: "gpt-X · N% left · /path"
    r"|^.*Auto\s+\(Gemini\s+\d+\).*\d+%\s+used\s*$"  # Gemini footer values row
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
    r"|Allow (?:this|execution of)"             # codex / gemini "Allow execution of [...]?"
    r"|Apply this change\?"                     # gemini edit-tool dialog
    r"|[›❯●•✦]\s*\d+\.\s*(?:Yes|Allow|Trust|Approve|Modify|No)"
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

    def _post_upload_summary(channel, uploaded, failed, thread_ts):
        lines = []
        if uploaded:
            lines.append(f":outbox_tray: Uploaded {len(uploaded)} file(s).")
        if failed:
            lines.append("Issues:\n  • " + "\n  • ".join(failed))
        if not lines:
            lines.append("Nothing to upload.")
        post(channel, "\n".join(lines), thread_ts)

    def _run_direct_uploads(channel, resolved, thread_ts=None):
        """Direct-to-Slack flow: file → as-is, folder → zip (junk excluded)."""
        uploaded, failed = [], []
        scope_failure = False
        for path in resolved[:20]:
            if scope_failure:
                break
            if os.path.isdir(path):
                base_name = os.path.basename(os.path.abspath(path)) or "archive"
                pre_size = directory_size_for_zip(path)
                if pre_size > ZIP_MAX_BYTES:
                    mb = pre_size / (1024 * 1024)
                    cap_mb = ZIP_MAX_BYTES / (1024 * 1024)
                    failed.append(
                        f"`{base_name}/` ({mb:.0f} MB after excluding junk > "
                        f"{cap_mb:.0f} MB Slack cap; use `!upload --link {path}` "
                        f"to send via pixeldrain instead)"
                    )
                    continue
                tf = tempfile.NamedTemporaryFile(
                    suffix=".zip", prefix="slack_upload_", delete=False)
                tf.close()
                zip_path = tf.name
                try:
                    nfiles = zip_directory(path, zip_path)
                    if nfiles == 0:
                        failed.append(f"`{base_name}/` (no files to zip after exclusions)")
                        continue
                    try:
                        app.client.files_upload_v2(
                            channel=channel, file=zip_path,
                            title=f"{base_name}.zip",
                        )
                        uploaded.append(f"{base_name}.zip ({nfiles} files)")
                    except SlackApiError as e:
                        err = e.response.get("error", "?")
                        if err == "missing_scope":
                            failed.append(
                                f"`{base_name}.zip` — bot needs `files:write` "
                                "scope; add it in OAuth & Permissions and reinstall.")
                            scope_failure = True
                        else:
                            failed.append(f"`{base_name}.zip` ({err})")
                except Exception as e:
                    failed.append(f"`{base_name}/` (zip error: {e})")
                finally:
                    try:
                        os.remove(zip_path)
                    except OSError:
                        pass
                continue
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
                    scope_failure = True
                    continue
                failed.append(f"`{os.path.basename(path)}` ({err})")
        return uploaded, failed

    def _run_link_upload(channel, source_path, thread_ts):
        """pixeldrain flow: build a password-protected zip, PUT, post link."""
        if not (os.path.isfile(source_path) or os.path.isdir(source_path)):
            post(channel, f":warning: `{source_path}` (not found)", thread_ts)
            return
        base = os.path.basename(os.path.abspath(source_path)) or "archive"
        # Strip a single trailing .zip from the base when wrapping a zip, so
        # the encrypted wrapper is named `<base>.zip` not `<base>.zip.zip`.
        if base.lower().endswith(".zip"):
            base = base[:-4]
        placeholder = post(channel,
                           f":lock: Building encrypted zip of `{base}` for link upload…",
                           thread_ts)
        ts = placeholder["ts"] if isinstance(placeholder, dict) else None
        # Use a temp DIRECTORY (not file) so the destination zip path doesn't
        # exist yet when `zip` opens it — zip treats an empty existing file as
        # an invalid archive (rc=3 "Zip file structure invalid").
        tmpdir = tempfile.mkdtemp(prefix=f"slack_link_{base}_")
        zip_path = os.path.join(tmpdir, f"{base}.zip")
        try:
            try:
                nfiles = make_encrypted_zip_for_upload(
                    source_path, zip_path, LINK_UPLOAD_PASSWORD)
            except Exception as e:
                msg = f":warning: zip failed: `{e}`"
                if ts:
                    update(channel, ts, msg)
                else:
                    post(channel, msg, thread_ts)
                return
            try:
                size_mb = os.path.getsize(zip_path) / (1024 * 1024)
            except OSError:
                size_mb = 0.0
            up_msg = (f":outbox_tray: Uploading `{base}.zip` ({nfiles} files, "
                      f"{size_mb:.1f} MB) to a public file host "
                      f"(trying {', '.join(DEFAULT_UPLOADER_ORDER[:3])}…)")
            if ts:
                update(channel, ts, up_msg)
            else:
                post(channel, up_msg, thread_ts)
            try:
                # link_upload tries the configured fallback chain; the
                # first host that doesn't time out / error wins.
                host_name, link = link_upload(zip_path)
            except Exception as e:
                msg = (f":warning: every link host failed: `{e}`. "
                       f"Try `!upload --direct {source_path}` instead, or "
                       f"set `LINK_UPLOAD_HOSTS` in `.env` to a working host.")
                if ts:
                    update(channel, ts, msg)
                else:
                    post(channel, msg, thread_ts)
                return
            # Per-host retention disclaimer:
            retention = {
                "pixeldrain-post": "~60 days from last view",
                "pixeldrain-put":  "~60 days from last view",
                "catbox":          "permanent (no auto-expiry)",
                "0x0":             "retention scales with file size; small files persist longer",
            }.get(host_name, "see host's policy")
            done_msg = (f":link: *{base}.zip* uploaded via *{host_name}*\n"
                        f"  • Link: {link}\n"
                        f"  • Password: `{LINK_UPLOAD_PASSWORD}`\n"
                        f"  • {nfiles} files, {size_mb:.1f} MB. "
                        f"Retention: {retention}. "
                        f"Anyone with the link can download but needs the "
                        f"password to extract.")
            if ts:
                update(channel, ts, done_msg)
            else:
                post(channel, done_msg, thread_ts)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

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
                # NOTE: the CLISession record is registered inside
                # start_named_session BEFORE the long tmux boot, so a
                # `!sessions` typed in the agent channel mid-startup sees
                # the in-progress session. Don't re-register here.
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

        if cmd == "!debug":
            # Dump the bridge's internal state — pid, uptime, in-memory
            # sessions dict, and any orphan tmux sessions matching the
            # agent-channel pattern. Use this when sessions appear to have
            # been lost or behavior is unexplained.
            now = time.time()
            try:
                pid = os.getpid()
                with open(f"/proc/{pid}/stat") as f:
                    stat = f.read().split()
                # field 22 (0-indexed 21) is starttime in clock ticks since boot
                clk_tck = os.sysconf("SC_CLK_TCK")
                with open("/proc/uptime") as f:
                    sys_uptime = float(f.read().split()[0])
                proc_start = sys_uptime - (int(stat[21]) / clk_tck)
                uptime = int(proc_start)
            except Exception:
                pid = os.getpid()
                uptime = -1
            lines = [
                f"*Bridge debug* — pid `{pid}`, uptime `{uptime}s`, "
                f"bot `<@{BRIDGE_BOT_USER_ID or '?'}>`",
            ]
            with sessions_lock:
                snapshot = list(sessions.items())
            if snapshot:
                lines.append(f"\n*In-memory sessions ({len(snapshot)}):*")
                for ch_id, s in snapshot:
                    age = int(now - s.last_used)
                    lines.append(
                        f"  • `{ch_id}` — `{s.cli}`, "
                        f"tmux `{s.tmux_name}`, idle {age}s"
                    )
            else:
                lines.append("\n*In-memory sessions:* none")
            try:
                tmux_list = subprocess.check_output(
                    ["tmux", "ls", "-F", "#S"], text=True,
                    stderr=subprocess.DEVNULL,
                ).strip().split("\n")
            except subprocess.CalledProcessError:
                tmux_list = []
            tracked = {s.tmux_name for _, s in snapshot}
            orphans = [n for n in tmux_list
                       if n and NAMED_CHANNEL_RE.match(n) and n not in tracked]
            if orphans:
                lines.append(
                    f"\n*Orphan agent-pattern tmux sessions "
                    f"(no in-memory record):*")
                for n in orphans[:20]:
                    lines.append(f"  • `{n}`")
                if len(orphans) > 20:
                    lines.append(f"  • … +{len(orphans) - 20} more")
            post(channel, "\n".join(lines), thread_ts)
            return

        if cmd in ("!raw", "!pane", "!tail"):
            # Dump the last N lines of the cleaned pane — escape hatch when
            # send_and_wait's chrome clipping ate something we wanted to see.
            parts_raw = text.split()
            n = 60
            if len(parts_raw) > 1:
                try:
                    n = max(1, min(int(parts_raw[1]), 500))
                except ValueError:
                    post(channel,
                         "Usage: `!raw [N]` — last N lines of the cleaned pane "
                         "(default 60, max 500).",
                         thread_ts)
                    return
            with sessions_lock:
                sess = sessions.get(channel)
            if not sess:
                post(channel, "No active session.", thread_ts)
                return
            tail = pane_tail(sess.tmux_name, n=n)
            if not tail.strip():
                post(channel, "_(pane is empty after cleanup)_", thread_ts)
                return
            post(channel, _format_for_slack(tail), thread_ts)
            return

        if cmd in ("!upload", "!file", "!files"):
            # Upload local file(s)/folder(s) to Slack — direct or via pixeldrain.
            # Relative paths are resolved against the active session's cwd; globs
            # are expanded. Single-path form prompts the user to pick the
            # destination unless --direct or --link is given.
            raw_parts = text.split()[1:]
            mode = None
            parts = []
            for tok in raw_parts:
                if tok in ("--direct", "--slack"):
                    mode = "direct"
                elif tok in ("--link", "--transfer", "--share"):
                    mode = "link"
                else:
                    parts.append(tok)
            if not parts:
                post(channel,
                     "Usage: `!upload <path>` (file, folder, or glob). "
                     "Single-path form will prompt you to pick:\n"
                     "  `1` — direct to Slack (folder is zipped, no password)\n"
                     f"  `2` — public-link host (zipped + password `{LINK_UPLOAD_PASSWORD}`, "
                     f"tries pixeldrain → catbox → 0x0)\n"
                     "Skip the prompt with `!upload --direct <path>` or "
                     "`!upload --link <path>`. Multiple paths always go direct.",
                     thread_ts)
                return
            with sessions_lock:
                sess = sessions.get(channel)
            base = sess.path if (sess and sess.path) else os.getcwd()
            resolved = []
            for p in parts:
                full = p if os.path.isabs(p) else os.path.join(base, p)
                matches = glob.glob(full)
                if matches:
                    resolved.extend(matches)
                else:
                    resolved.append(full)  # so we can report it as missing

            single = len(resolved) == 1 and (
                os.path.isfile(resolved[0]) or os.path.isdir(resolved[0])
            )
            if mode is None and single:
                # Stash pending state and prompt with a 1/2 menu.
                src = resolved[0]
                kind = "folder" if os.path.isdir(src) else "file"
                size_note = ""
                if kind == "folder":
                    sz = directory_size_for_zip(src) / (1024 * 1024)
                    size_note = f" ({sz:.0f} MB after excluding junk)"
                else:
                    try:
                        size_note = f" ({os.path.getsize(src) / (1024 * 1024):.1f} MB)"
                    except OSError:
                        pass
                with pending_uploads_lock:
                    pending_uploads[channel] = {
                        "source": src,
                        "expires_at": time.time() + PENDING_UPLOAD_TTL_SEC,
                    }
                post(channel,
                     f":file_folder: Upload `{os.path.basename(src) or src}` "
                     f"({kind}){size_note}?\n"
                     f"  `1` — direct to Slack (no password, Slack 1 GB cap)\n"
                     f"  `2` — public-link host (encrypted zip with password "
                     f"`{LINK_UPLOAD_PASSWORD}`; tries "
                     f"{' → '.join(DEFAULT_UPLOADER_ORDER[:3])} in order)\n"
                     f"_Reply `1` or `2` within {PENDING_UPLOAD_TTL_SEC // 60} min "
                     f"(or `!1` / `!2` if a CLI dialog is open). The pending pick "
                     f"falls through to the CLI session otherwise._",
                     thread_ts)
                return

            if mode == "link":
                if len(resolved) != 1 or not (
                    os.path.isfile(resolved[0]) or os.path.isdir(resolved[0])
                ):
                    post(channel,
                         ":warning: `!upload --link` needs exactly one existing "
                         "file or folder.", thread_ts)
                    return
                _run_link_upload(channel, resolved[0], thread_ts)
                return

            # mode == "direct" or multi-path or single-path-not-found:
            # run the direct-Slack flow.
            uploaded, failed = _run_direct_uploads(channel, resolved, thread_ts=thread_ts)
            _post_upload_summary(channel, uploaded, failed, thread_ts)
            return

        if cmd in ("!1", "!2") or (text.strip() in ("1", "2")):
            # Pending !upload menu pick. Only fires if the channel has a pending
            # entry; otherwise falls through to the CLI session forwarding below
            # so codex/claude permission dialogs that say "1. Yes / 2. No" still
            # work normally.
            pick = text.strip().lstrip("!")
            if pick in ("1", "2"):
                pending = _take_pending_upload(channel)
                if pending is not None:
                    src = pending["source"]
                    if pick == "1":
                        uploaded, failed = _run_direct_uploads(
                            channel, [src], thread_ts=thread_ts)
                        _post_upload_summary(channel, uploaded, failed, thread_ts)
                    else:
                        _run_link_upload(channel, src, thread_ts)
                    return
                # No pending — fall through to whatever else (CLI session).

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
                 "`!upload <path>` — upload local file(s) or folder(s). "
                 "Single-path form prompts you to pick:\n"
                 "    `1` — direct to Slack (folder zipped, no password, "
                 "junk like `.git`/`__pycache__`/`node_modules`/`.venv`/`*.pyc` excluded)\n"
                 f"    `2` — public-link host (encrypted zip with password "
                 f"`{LINK_UPLOAD_PASSWORD}`; tries "
                 f"{' → '.join(DEFAULT_UPLOADER_ORDER[:3])} in order, "
                 f"first one that works wins)\n"
                 "Skip the prompt with `!upload --direct <path>` or "
                 "`!upload --link <path>`. Multiple paths or globs always go direct.\n"
                 "`!download` (alias `!dl`) — attach a file to your message + include "
                 "`!download` to save it into the session's cwd. Useful for sharing "
                 "screenshots or PDFs with the agent.\n"
                 "\n"
                 "*Other*\n"
                 "`!sessions` (alias `!ls`) — list every active bridge session globally\n"
                 "`!debug` — dump bridge pid, uptime, in-memory sessions, and any "
                 "orphan tmux sessions matching the agent-channel pattern. Use this "
                 "when sessions look lost or behavior is unexplained.\n"
                 "`!raw [N]` (alias `!pane`, `!tail`) — dump the last N lines of the "
                 "cleaned tmux pane (default 60, max 500). Useful when a streaming/"
                 "monitor command's output got clipped to `(no output)`.\n"
                 "`!check_limit` — usage % for Claude, Gemini, Codex with reset times\n"
                 "`!kill-server` — `tmux kill-server`: wipe ALL bridge sessions and "
                 "archive their channels. Use when sessions are stuck or you want a "
                 "clean slate.\n"
                 "`!help` — this message\n"
                 "\n"
                 "*Safety:* every CLI is launched in a stricter mode where any "
                 "model-initiated mutation (file write, delete, shell command outside "
                 "a tiny read-only allowlist) triggers a permission dialog that the "
                 "bridge forwards here — even when the model thinks you authorized it. "
                 "Reply with the option number (`1`, `2`, …) or `y` / `n` to decide. "
                 "`!run` is *local* shell on the host; mind what you type — it does "
                 "NOT go through any agent approval.\n"
                 "*Lifetime:* sessions persist until you `!end` / `!reset` / `!kill-server` "
                 "(no idle timeout).",
                 thread_ts)
            return

        # Unknown bridge command — refuse to forward. Otherwise a typo like
        # `!session` (singular) would be typed into the active CLI as raw
        # text, which on gemini flips it into shell-mode and breaks every
        # subsequent free-text message until the user notices and !cancels.
        if cmd.startswith("!"):
            post(channel,
                 f"Unknown bridge command `{cmd}`. Try `!help` for the full "
                 f"list. (If you really meant to send `{cmd}` to the CLI, "
                 f"prefix it with a space — the bridge only intercepts `!` "
                 f"at the very start of the message.)",
                 thread_ts)
            return

        with sessions_lock:
            sess = sessions.get(channel)
        if not sess:
            # Recovery path: maybe this is an agent channel whose tmux
            # session is still alive but our in-memory record was lost
            # (bridge restart, socket race). Re-link silently if possible.
            sess = try_relink_session(channel, app)
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
            # Three-tier fallback when send_and_wait's chrome clipping leaves
            # the response empty (common during long tool-call sequences where
            # the new turn's content is mostly tool-call chrome that
            # clean_output strips). Captured under the io_lock because the
            # tmux pane is shared state for the session.
            #
            #   1. Permission dialog waiting? Surface it — the user can't
            #      proceed without answering, so it's the actionable message.
            #   2. Otherwise anchor a tail at the user's last echoed input;
            #      shows recent activity instead of stale content above.
            #   3. Otherwise tell them the agent is still working — don't
            #      dump the bottom of the pane (it's frequently a stale
            #      previous response).
            if not response.strip():
                dialog = extract_pending_dialog(sess.tmux_name)
                if dialog:
                    # _format_for_slack will wrap the whole thing in a code
                    # block when it sees newlines, so we put the header inside
                    # too — don't add inner fences (would double-wrap).
                    response = (
                        f"PERMISSION DIALOG ({sess.cli}) — reply with the "
                        f"option (!1, !2, …):\n\n{dialog}"
                    )
                else:
                    recent = pane_tail_after_user_input(
                        sess.tmux_name, text, max_lines=20)
                    if recent.strip():
                        response = (
                            f"AGENT STILL RENDERING ({sess.cli}) — recent "
                            f"activity:\n\n{recent}"
                        )
                    else:
                        # Single-line short → not auto-wrapped → backticks
                        # render as inline code in Slack.
                        response = (
                            f":hourglass_flowing_sand: `{sess.cli}` busy; "
                            f"`!cancel` to abort or `!raw 60` to peek."
                        )

        update(channel, ts, _format_for_slack(response))

    return handle


def main():
    # _load_dotenv() already ran at module load time so module-level
    # constants (LINK_UPLOAD_PASSWORD etc) could read their overrides.
    # No need to call it again here.
    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    app_token = os.environ.get("SLACK_APP_TOKEN")
    if not bot_token or not app_token:
        sys.exit("Set SLACK_BOT_TOKEN and SLACK_APP_TOKEN (env vars or .env file). See .env.example.")

    app = App(token=bot_token)
    handle = make_handler(app)

    # Identify our own bot's user id so we can skip its own messages without
    # also skipping user-token-posted messages (Slack tags any message that
    # comes through this OAuth app — including xoxp- posts via the user
    # token — with bot_id, so filtering on `bot_id` alone drops legitimate
    # user messages). Module-global so any handler can read it.
    global BRIDGE_BOT_USER_ID
    try:
        BRIDGE_BOT_USER_ID = app.client.auth_test()["user_id"]
        print(f"Bridge bot user id: {BRIDGE_BOT_USER_ID}")
    except Exception as e:
        print(f"warning: auth_test failed: {e}; falling back to bot_id filter")
        BRIDGE_BOT_USER_ID = None

    @app.event("message")
    def on_message(event, logger):
        # Skip our own bot's posts — filter by user id, not by bot_id
        # presence. (xoxp- user-token posts ALSO carry bot_id because they
        # go through this app's OAuth.)
        author_user = event.get("user")
        if BRIDGE_BOT_USER_ID and author_user == BRIDGE_BOT_USER_ID:
            return
        if BRIDGE_BOT_USER_ID is None and event.get("bot_id"):
            return  # legacy fallback if auth_test failed at startup
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

    # Socket-failure watchdog. slack_bolt's Socket Mode auto-reconnects on
    # transient errors but a degraded connection can leak hours of dropped
    # events before recovering. We tail the stderr stream of slack_bolt's
    # logger; if we see >= 5 socket-state failures within 60s, exit so an
    # external supervisor (systemd, nohup loop, tmux respawn) can bring up
    # a fresh process. Using a logging.Handler here avoids re-implementing
    # error parsing.
    import logging as _logging
    _sock_err_times = []

    class _SocketWatchdog(_logging.Handler):
        def emit(self, record):
            msg = record.getMessage()
            if "Failed to check the state of sock" not in msg:
                return
            now = time.time()
            _sock_err_times.append(now)
            # Drop entries older than 60s.
            while _sock_err_times and now - _sock_err_times[0] > 60:
                _sock_err_times.pop(0)
            if len(_sock_err_times) >= 5:
                print(f"[watchdog] {len(_sock_err_times)} socket failures in "
                      f"60s — exiting so a supervisor can restart us.")
                # Hard exit so any threads/sockets are torn down immediately.
                os._exit(1)

    _logging.getLogger().addHandler(_SocketWatchdog())

    print("Slack bridge running. DM your bot or @mention it in a channel.")
    SocketModeHandler(app, app_token).start()


if __name__ == "__main__":
    main()
