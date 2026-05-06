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
| `cmd` | argv launched in tmux. `codex` uses `-s workspace-write -a untrusted` (sandbox writes to cwd; auto-approves only a small read-only allowlist, asks for everything else — see Safety posture below and comments at `main.py:171`). |
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
| `!raw [N]` / `!pane` / `!tail` | Last N lines of the cleaned tmux pane (default 60, max 500). Same `clean_output` ANSI/box/spinner stripping as the regular reply path, but **no chrome clipping** — so it shows what `send_and_wait` would have eaten. Use when a streaming/monitor command's reply came back as `(no output)`. |
| `!debug` | Dump the bridge's internal state: pid, uptime, in-memory sessions dict, and any orphan tmux sessions matching the agent-channel pattern (`<cli>-<slug>-<uniqid>`) but not currently tracked. Use when sessions appear lost or behavior is unexplained. |
| `!check_limit` / `!limits` / `!check` | Run the `check_limit` parsers in parallel (with a sequential retry for any CLI that came back short) and post a Block Kit summary. |
| `!upload <path>` | Upload file(s)/folder(s). Single-path form prompts you to pick: `1` (Slack direct — zips folders, no password, 1 GB cap, junk excluded) or `2` (pixeldrain — encrypted zip with password = `LINK_UPLOAD_PASSWORD` env var, public link ~60 days from last view, no Slack size cap). Skip the prompt with `!upload --direct <path>` or `!upload --link <path>`. Multi-path / glob always goes direct. Junk excluded: `.git`/`__pycache__`/`node_modules`/`.venv`/`venv`/`.tox`/`.mypy_cache`/`.pytest_cache`/`*.pyc`/`*.pyo`/`.DS_Store`. |
| `1` / `!1` / `2` / `!2` | Pick the option for the most recent `!upload` in this channel (within 2 min). Plain `1`/`2` falls through to the active CLI session if there's no pending menu — so codex/claude permission dialogs still work. |
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

**Empty-extraction fallback.** Streaming or monitor-style commands
re-render the input prompt below their output, so `CHROME_DIVIDER_RE`
clips everything and `send_and_wait` returns `""`. When that happens,
the dispatcher (`handle()` tail) falls back to `pane_tail(name, n=60)` —
last 60 lines of the cleaned pane, with no chrome clipping — prefixed
with a one-line note explaining the fallback. The user gets to see
output instead of "(no output)". `!raw [N]` exposes the same primitive
on demand.

## Output cleanup — `clean_output`

ANSI escapes, box-drawing characters, spinner-only lines, empty-prompt
lines (`›`, `❯`), and known noise (`✻ Brewed for 5s`,
`⎿  Running…`, `Press Esc to interrupt`) are all stripped. Leading
markers `● • ✦` are removed so chained tool-call lines don't render as
Slack bullet points. Multiple blank lines collapse to one.

## Safety posture

- All agents run inside tmux on the host where the bridge is running, so
  they can read/write the same FS as the bridge user.
- **Codex** is launched with `-s workspace-write` (sandboxed to cwd) and
  `-a untrusted` (auto-approves only a small allowlist of read-only
  commands — `find`, `sort`, `ls`, `cat`, `head`, `tail`, `wc`, `grep`,
  `rg`, `pwd`. Anything else — including any model-decided mutation —
  triggers a permission dialog in the codex pane, which the bridge
  detects with `PERMISSION_DIALOG_RE` and forwards to Slack. Reply with
  the option number to decide.
  - `-a untrusted` was deliberately chosen over the more permissive
    `-a on-request` in commit `db5bc87` and re-chosen here: with
    `on-request` the model treated explicit user phrasing like
    "delete X" as authorization and ran without raising a dialog at
    all, which broke the bridge's "ask in Slack" promise. The cost is
    more prompts for routine commands; the benefit is that no
    model-initiated destructive action can run without a Slack
    approval.
- **Claude / Gemini** rely on each CLI's own default per-tool prompts
  (Bash, Edit, Write, etc.). Those prompts surface in the tmux pane
  and the bridge forwards them like any other dialog.
- The bridge **never types into a permission dialog itself**. The only
  auto-keystroke is the *first-launch folder-trust* dialog (handled
  once in `start_session`), which only unlocks file access — it does
  not blanket-approve mutations.
- `!run` is local shell on the host, executed directly by the bridge
  in the session's cwd. It does NOT go through any agent approval, so
  treat it like an interactive terminal.
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

## Recovery from bridge state loss

The bridge keeps `sessions` in process memory only — a restart wipes the
dict, even though the underlying `tmux` sessions usually survive. Two
mechanisms handle that gap:

1. **Auto-relink (silent).** When a free-text message arrives in a channel
   with no `sessions[channel]` entry, `try_relink_session(channel, app)`
   does:
   - `conversations.info(channel)` to fetch the channel name.
   - Match against `^(gemini|codex|claude)-.+-[0-9a-f]{4}$`
     (`NAMED_CHANNEL_RE`).
   - If matched, `session_exists(name)` checks for a live tmux pane.
   - If yes, build a new `CLISession(cli=…, tmux_name=name, path=None,
     slack_channel_id=channel, is_named=True)` and insert under
     `sessions_lock`. The handler dispatch then continues normally.
   `path` and `extra_args` are unrecoverable (they only existed in the
   prior bridge's memory) — that affects `!run`'s default cwd and a
   model flag's persistence across `!reset`. Accepted tradeoff.
2. **Socket watchdog (process-level).** `slack_bolt`'s Socket Mode
   auto-reconnects on transient errors but has been observed to leave
   the bridge alive-but-degraded for hours after persistent SSL drops.
   `main()` installs a `logging.Handler` that watches for
   `"Failed to check the state of sock"` lines; 5 of them within 60s
   triggers `os._exit(1)` so an external supervisor (systemd /
   `nohup` loop / tmux respawn) can bring up a fresh process. Auto-relink
   then heals everything mid-flight as users continue messaging.

When something looks off, ask the user to send `!debug` — it dumps pid,
uptime, the in-memory sessions dict, and any orphan tmux sessions whose
name matches the agent-channel pattern but isn't currently tracked.
Pasting that output back is enough to diagnose most state-loss cases.

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
- **`on_message` filters by bot user id, not by `bot_id` presence.** Slack
  tags any message that goes through this OAuth app — including
  user-token (`xoxp-`) posts — with a `bot_id`. Filtering on
  `event.get("bot_id")` would drop legitimate user-token posts. The
  bridge calls `auth_test()` once at startup, caches `BRIDGE_BOT_USER_ID`,
  and skips events where `event.get("user") == BRIDGE_BOT_USER_ID`.
  This is what lets `tests/slack_drive.py` drive the bridge as a real
  human via the user token.

## Testing

Three layers, increasing fidelity and cost:

### `tests/test_handlers.py` — unit tests (fastest)

Build a `FakeApp` whose `client` records every Slack API call into in-memory
lists. Mock `tmux`/`subprocess`/`urllib` at the module boundary. Assert on
recorded calls and on the bridge's outgoing posts/updates.

```bash
python3 -m unittest tests.test_handlers -v        # ~80 tests, <1s
```

### `tests/sim_user.py` — in-process sim against real CLIs

Same `FakeApp` (no Slack), but exercises real `tmux` + real `gemini` to
catch issues that pure mocks would miss: race windows during named-session
startup, the chrome-strip behavior on real gemini output, the typo guard
under a real CLI session, etc.

```bash
python3 tests/sim_user.py                          # ~30s, runs real gemini briefly
```

### `tests/slack_drive.py` — end-to-end Slack drive (highest fidelity)

Posts as you (`xoxp-` user token) into a fresh `#<cli>-test_<timestamp>`
private channel; the bridge replies as the bot; the driver polls
`conversations.history` and asserts on what came back. Validates the full
Slack round-trip including event delivery, scopes, identity overrides, etc.

```bash
python3 tests/slack_drive.py                                 # codex, default cwd
python3 tests/slack_drive.py --cli gemini --skip-roundtrip   # cheaper (no agent API call)
python3 tests/slack_drive.py --keep                          # don't archive at end
```

Requires `SLACK_USER_TOKEN=xoxp-…` in `.env` with these user-token scopes:

```
chat:write, channels:history, groups:history, im:history,
channels:read, groups:read, im:read, files:read, files:write,
groups:write
```

(The bot scopes documented above stay unchanged.) The driver does not
need `im:write` — DMs are not used.

#### Slack-side gotchas the driver had to work around

1. **Only bot-created private channels deliver events.** When a *user*
   creates a private channel and invites the bot, the bot ends up listed
   as a member but its Socket Mode connection silently drops `message.groups`
   events for that channel. The driver therefore creates the test channel
   via the bot token, then invites the user. Same OAuth app, same workspace
   — but the route by which the bot joined determines whether events flow.
2. **Subscription propagation lag (~10–20s).** After the bot creates a
   channel + invites the user, Slack needs ~10–20s before user-posted
   messages reliably reach the bot's socket. *Active* probing during
   that window (posting `!sessions` repeatedly) seems to confuse delivery
   further. A *passive* `time.sleep(20)` before the first post is more
   reliable than any retry pattern we tried.
3. **Branded posts don't carry a `user` field.** When the bridge posts
   with `chat:write.customize` (custom `username`/`icon_emoji`, used for
   the agent channel's "ready" message and for "CLI Bridge — Gemini"
   identity), Slack returns `subtype: "bot_message"` with `user: None`.
   The driver's bot-message filter accepts `subtype == "bot_message"` in
   addition to `user == bot_user_id`.
4. **`chat.postMessage` to an archived channel raises `is_archived`.**
   The bridge auto-archives named channels on `!end`, so the driver's
   `!end` step uses `max_attempts=1` (a retry would land in an archived
   channel). All `post()` calls in `send_and_wait` are wrapped in a
   `try/except SlackApiError` that returns `None` on post failure
   instead of bubbling up.
