# slack_bridge

A Slack Bolt app (Socket Mode) that forwards Slack messages to a
`claude` / `gemini` / `codex` CLI running in a per-channel `tmux`
session, then posts the CLI's reply back to Slack.

## High-level flow

```
Slack message ──▶ on_message handler ──▶ make_handler.handle(...)
                                                │
                                                ├─ command? (!gemini, !end, !upload, …)
                                                │
                                                └─ plain text:
                                                      send_and_wait(tmux_session, text, cli)
                                                          → send-keys + Enter
                                                          → poll capture-pane until stable
                                                          → extract assistant turn
                                                          → clean ANSI / box / chrome
                                                      ──▶ chat.update placeholder
```

Everything in `src/slack_bridge/main.py` (~960 lines, single file).

## Session model

```python
@dataclass
class CLISession:
    cli: str                     # "gemini" | "codex" | "claude"
    tmux_name: str               # name of the tmux session running the CLI
    last_used: float             # idle timeout tracking
    path: Optional[str]          # cwd, when started via "!<cli> <name> <path>"
    slack_channel_id: Optional[str]
    is_named: bool               # True when this session owns its own Slack channel
    io_lock: threading.Lock      # serializes typing into one tmux session
```

`sessions` is a `dict` keyed by Slack `channel_id`. DM channels are
unique per user, so the channel id alone identifies a per-user DM
session. Named sessions live in their own (private) channel.

**Sessions persist indefinitely** — there is no idle reaper. They live
until `!end`, `!reset` (which restarts in place), `!kill-server`, or a
host restart. `cleanup_idle_loop` is still defined for reference but is
not spawned by `main()`; `IDLE_TIMEOUT_SEC` is `None`.

## CLI configs (`CLI_CONFIGS`)

Per-CLI knobs in one dict:

| Key | Purpose |
| --- | --- |
| `cmd` | argv launched in tmux. `codex` uses `-s workspace-write -a on-request` (sandbox writes to cwd, on-request approval — see comments at `main.py:156`). |
| `ready_marker` | Substring that proves the input prompt is up (start_session waits for it). |
| `assistant_marker` | Glyph the CLI prefixes assistant turns with (`✦` Gemini, `•` Codex, `●` Claude). Used to detect new turns. |
| `trust_pattern` / `trust_keys` | First-launch folder-trust dialog detection + keystrokes to dismiss it. Auto-handled in `start_session`. |
| `display_name` / `icon_emoji` | Bot identity for posts in named agent channels. Requires `chat:write.customize`; falls back gracefully if missing. |
| `model_flag` | Flag the CLI uses to pick a model (`-m` for gemini/codex, `--model` for claude). Used by `!model`. |

## Slash commands

Recognized at the start of a message:

| Command | Effect |
| --- | --- |
| `!gemini` / `!codex` / `!claude` | Start (or restart) a session in the *current* channel. |
| `!gemini <name> <path>` (also `!codex` / `!claude`) | Create private channel `<cli>-<slug>-<uniqid>` (e.g. `codex-cctv_date_rename-b93c`), invite the user, launch the CLI with `cwd=<path>`. The tmux session uses the same name. |
| `!end` | Kill the tmux session. Archives the channel if it's a named session. |
| `!reset` | Restart in place: same cli, cwd, tmux name, and `extra_args` (model). No `!end` first — the session record is preserved. |
| `!cancel` / `!interrupt` / `!stop` | `tmux send-keys ... Escape` — interrupt the running CLI turn without killing it. |
| `!switch <cli>` | Kill current tmux, relaunch with a different CLI, keep cwd + tmux name. Clears `extra_args` (model flags don't carry across CLIs). |
| `!model <name>` | Kill current tmux, relaunch the same CLI with `<model_flag> <name>` appended. With no arg, prints current `extra_args`. |
| `!run <cmd>` | `subprocess.run(cmd, shell=True, cwd=session.path, timeout=30)`. Local shell — does not involve the agent. Output truncated at 6KB per stream. |
| `!status` | Show what's running in this channel. |
| `!sessions` / `!ls` / `!list` | Snapshot every active session globally (channel link, cli, idle, tmux name, cwd, extra_args). |
| `!check_limit` / `!limits` / `!check` | Run the `check_limit` parsers in parallel (with a sequential retry for any CLI that came back short) and post a Block Kit summary. |
| `!upload <path>` | `files_upload_v2` for absolute, session-relative, or glob paths. Caps at 20 files. Needs `files:write`. |
| `!download` / `!dl` | Pull files attached to the same Slack message into the session's cwd via `url_private_download` + bot-token auth. Caps at 10 files; sanitizes filenames to `basename`. |
| `!kill-server` / `!nuke` | `tmux kill-server`: wipe ALL bridge sessions and archive their channels. |
| `!help` | Print the command list. |
| anything else | Forwarded to the active session (or "no active session" message). |

## `send_and_wait` — the response detector

Heart of the bridge (`main.py:353`). Sends the user text + Enter, then
polls `capture-pane` until the pane stops changing. Two settle
conditions, whichever fires first:

- **`response_stable_secs` (3.5s)** with a *new* assistant marker visible
  → CLI just produced an answer.
- **`idle_stable_secs` (8s)** with no new marker AND no spinner →
  permission/tool prompt is waiting on the user. Forward it to Slack so
  the user can answer there (`y`, `1`, etc).

Extraction has two modes:

- *new turn produced* → start from the *last* `assistant_marker`.
- *no new turn yet* (we're at a permission dialog) → start from the
  user's echoed input. Important: using `rfind(marker)` here would point
  at the *previous* turn and leak it into the reply.

Then `CHROME_DIVIDER_RE` clips the input chrome below the conversation.
But if `PERMISSION_DIALOG_RE` finds a dialog under that chrome, we keep
the dialog (clipped at `DIALOG_END_RE`) so the user can read and answer
it in Slack.

`_is_responding()` deliberately does *not* match `"esc to cancel"` —
codex permission dialogs include that string in their footer, which would
keep the polling loop running forever. The dialog flow above handles it
instead.

## Output cleanup — `clean_output`

ANSI escapes, box-drawing characters, spinner-only lines, empty-prompt
lines (`›`, `❯`), and known noise (`✻ Brewed for 5s`,
`⎿  Running…`, `Press Esc to interrupt`) are all stripped. Leading
markers `● • ✦` are removed so chained tool-call lines don't render as
Slack bullet points. Multiple blank lines collapse to one.

## Safety posture

- All agents run inside tmux on the host where the bridge is running, so
  they can read/write the same FS as the bridge user.
- Codex is launched with `-s workspace-write` (sandboxed to cwd) and
  `-a on-request` (asks before non-trivial mutations). The bridge does
  not auto-approve — the user does, by replying in Slack.
- Slack tokens are loaded from `.env` at the repo root (or next to
  `main.py`); existing env vars take precedence. `.env` is gitignored.
- Concurrent Slack messages to the same channel are serialized through
  `CLISession.io_lock` so keystrokes can't interleave inside one tmux
  pane.

## Required Slack scopes

From the docstring at the top of `main.py`:

| Scope | Why |
| --- | --- |
| `app_mentions:read`, `chat:write`, `im:history`, `im:read`, `im:write` | Core message handling |
| `groups:write`, `groups:read`, `groups:history` | Named sessions (private channel per project) |
| `files:write` | `!upload` |
| `chat:write.customize` *(optional)* | Branded "CLI Bridge — Gemini" identity in named channels |

Subscribe the bot to `app_mention`, `message.im`, `message.groups`.

## Things that are easy to miss

- Don't add `--no-alt-screen` to the codex command — codex 0.114.0
  ignores stdin from `tmux send-keys` in inline mode.
- `chat.update` rejects updates on messages that were posted with a
  custom `username`/`icon_emoji`. The placeholder is therefore posted
  *without* branded identity; the branded identity only applies to fresh
  posts in named channels.
- Channel names always carry a `secrets.token_hex(2)` suffix (4 hex chars,
  ~65k space) so re-running with the same project name produces a fresh
  channel; on the rare collision we just retry with a new suffix (3 tries).
- The `!check_limit` integration loads `src/check_limit/main.py` via
  `importlib` under the name `check_limit_main` to dodge package-name
  collisions; cached via the module-level `_check_limit`.
