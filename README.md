# multi-agent

A small set of Python tools that drive AI coding CLIs (Claude Code, Gemini
CLI, Codex CLI) through `tmux` and screen-scrape their TUIs:

- **`slack_bridge`** — turns a Slack DM (or any channel) into a persistent
  chat with one of the CLIs. Each `(user, channel)` keeps its own tmux
  session so context persists across messages. Folder uploads, encrypted
  link sharing via pixeldrain, automatic recovery from bridge restarts.
- **`check_limit`** — polls each CLI's "show my quota" command and prints
  a one-line summary or draws a Tk widget that auto-refreshes.
- **`multi_agent_caption`** — caption every video in a CCTV dataset by
  extracting the middle frame and prompting Gemini + Codex in parallel
  with quota-aware fallback.

Per-component deep dives in [`docs/`](./docs/):
[overview](./docs/overview.md) ·
[slack_bridge](./docs/slack_bridge.md) ·
[check_limit](./docs/check_limit.md) ·
[multi_agent_caption](./docs/multi_agent_caption.md).

## Setup

```bash
# 1. Install deps (just slack-bolt + slack-sdk)
pip install -r requirements.txt

# 2. Copy .env.example → .env and fill in your values (see below)
cp .env.example .env
$EDITOR .env

# 3. Run the bridge (foreground for testing; nohup for daemon)
python3 src/slack_bridge/main.py
```

You also need on your `$PATH`:
- `tmux` — used as the I/O layer for every CLI
- One or more of: `claude`, `gemini`, `codex` — the actual AI CLIs
  (installed and authenticated separately; this repo only orchestrates).
- `zip` — used to build password-protected zips for `!upload --link`.

## Required environment variables

The bridge and the caption pipeline both load `.env` at module load time.
Set these in `.env` at the repo root.

| Var | Where to get it | Required? | Used by |
| --- | --- | --- | --- |
| `SLACK_BOT_TOKEN` | Slack app → OAuth & Permissions → "Bot User OAuth Token" (`xoxb-…`) | **yes** | bridge |
| `SLACK_APP_TOKEN` | Slack app → Basic Information → App-Level Tokens (`xapp-…`, scope `connections:write`) | **yes** | bridge (Socket Mode) |
| `SLACK_USER_TOKEN` | Slack app → OAuth & Permissions → "User OAuth Token" (`xoxp-…`) | optional | `tests/slack_drive.py` only — drives the bridge as a real human via Slack |
| `LINK_UPLOAD_PASSWORD` | freeform | optional (default `changeme`) | bridge (`!upload --link` zip password) |
| `MULTI_AGENT_CAPTION_DATASET` | path on your machine | optional (default `<repo>/datasets/videos`) | caption pipeline |

`.env.example` has commented placeholder lines for each. See it for full
context on the user-token scopes if you intend to run `slack_drive`.

## Required Slack app config

When creating the Slack app at https://api.slack.com/apps:

**Bot Token Scopes** (always needed):

```
app_mentions:read   chat:write           chat:write.customize
files:write         groups:history       groups:read
groups:write        im:history           im:read
im:write
```

**User Token Scopes** (only if you want `tests/slack_drive.py`):

```
chat:write            channels:history     groups:history
im:history            channels:read        groups:read
im:read               files:read           files:write
groups:write
```

**Event Subscriptions** (bot events): `app_mention`, `message.im`,
`message.groups`.

After adding scopes, click **Reinstall to Workspace** and copy the new
tokens to `.env`.

## Running

```bash
# Slack bridge (Socket Mode; one process per workspace)
python3 src/slack_bridge/main.py

# One-shot CLI usage summary
python3 src/check_limit/main.py

# Tk usage widget (auto-refreshes every 6 min)
python3 src/check_limit/widget.py

# Caption pipeline (long-running, resumable)
python3 src/multi_agent_caption/main.py
```

In Slack, after the bridge is running, DM your bot or `@`-mention it in a
channel to start. Send `!help` for the full command list. Useful starting
points:

- `!gemini <name> <path>` — create a private agent channel and launch
  gemini with `cwd=<path>` (also `!codex` / `!claude`)
- `!sessions` — list every active session globally
- `!debug` — dump bridge state when something looks off
- `!run <shell-cmd>` — run a shell command in the session's cwd (no agent
  involved, no tokens spent)

## Tests

Three layers, increasing fidelity and cost:

```bash
# Unit tests — fast, no Slack, no real CLIs
python3 -m unittest discover tests -p 'test_*.py' -v

# In-process sim — same handler the bridge runs, against real tmux + gemini
python3 tests/sim_user.py

# End-to-end Slack drive — posts as you, walks the test plan against the
# running bridge. Auto-creates a fresh #<cli>-test_<ts> private channel
# and archives it at the end. Requires SLACK_USER_TOKEN.
python3 tests/slack_drive.py --cli gemini --skip-roundtrip
```

See [docs/slack_bridge.md → Testing](./docs/slack_bridge.md#testing) for
which Slack-side gotchas each layer catches.

## Bridge robustness

The bridge keeps session state in process memory only, so restarts
normally lose everything. Two recovery paths handle that:

- **Auto-relink (silent).** Free-text in a channel with no in-memory
  session triggers a `conversations.info` lookup; if the channel name
  matches `<cli>-<slug>-<uniqid>` and a tmux session of that name is
  alive, the bridge rebuilds the in-memory record and forwards the
  message to the running agent. No user action required.
- **Socket watchdog.** Repeated socket-state failures (`SSLEOFError`)
  trigger `os._exit(1)` so an external supervisor (`nohup` loop,
  `systemd`, `tmux respawn`) can bring up a fresh process. Auto-relink
  heals state mid-flight as users keep messaging.

`!debug` in any channel dumps the bridge's pid, uptime, in-memory
sessions, and any orphan tmux sessions matching the agent-channel
pattern. Paste its output if behavior looks wrong.

## Safety

- Codex is launched with `-s workspace-write -a untrusted` so the model
  cannot run shell commands or write files without raising a permission
  dialog that the bridge forwards to Slack.
- `!upload --link` uploads to **pixeldrain** as a password-protected zip;
  anyone with the URL + password can download until pixeldrain expires
  the file (~60 days from last view). Set `LINK_UPLOAD_PASSWORD` in
  `.env` to your own value.
- `!run` is *local* shell on the bridge host, executed directly as the
  bridge user. It does NOT go through any agent approval. Treat it like
  an interactive terminal session for whoever can DM the bot.
- Sessions persist until you `!end` / `!reset` / `!kill-server` — there
  is intentionally no idle timeout.

## License

(none specified yet — see [`LICENSE`](./LICENSE) if/when added.)
