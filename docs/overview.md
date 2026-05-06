# multi-agent — overview

Two small Python tools that drive AI coding CLIs (Claude Code, Gemini CLI,
Codex CLI) through `tmux` and screen-scrape their TUIs:

```
src/
├── check_limit/        # poll usage % from each CLI
│   ├── main.py           CLI entry + parsers + tmux capture helper
│   └── widget.py         Tk desktop widget that reuses main.py
└── slack_bridge/       # Slack <-> CLI bridge with per-channel tmux sessions
    └── main.py
```

Both pieces share the same trick: launch a CLI inside a detached `tmux`
session, send it keystrokes (`tmux send-keys`), and read back the rendered
pane (`tmux capture-pane`). That's the whole "API" — there's no SDK or
process IPC. Everything else is parsing of box-drawing characters,
spinners, and ANSI noise.

## Components

- [`check_limit`](./check_limit.md) — runs each CLI's "show my quota"
  command (`claude /usage`, `gemini /model`, `codex` + `/status`), parses
  the rendered panel, prints a one-line summary or draws a Tk widget that
  refreshes every 60s.
- [`slack_bridge`](./slack_bridge.md) — a Slack Bolt app. A DM (or any
  channel the bot is invited to) becomes a persistent chat with the chosen
  CLI: each `(user, channel)` keeps its own tmux session so context
  persists across Slack messages.

## Why tmux at all

The CLIs are TUIs designed for a real terminal — they refuse to render
properly when piped, hide output behind alt-screens, and need a live PTY
to accept slash commands. Detached tmux sessions give us that PTY plus
free scrollback, and `capture-pane` is a stable read-back interface.

## Setup

```bash
pip install -r requirements.txt          # only slack-bolt
cp .env.example .env                      # fill SLACK_BOT_TOKEN / SLACK_APP_TOKEN

# Run pieces individually
python3 src/check_limit/main.py           # one-shot summary
python3 src/check_limit/widget.py         # Tk widget, auto-refresh 60s
python3 src/slack_bridge/main.py          # Slack socket-mode app
```

The CLIs themselves (`claude`, `gemini`, `codex`) must be installed and
authenticated separately — this repo only orchestrates them. `tmux` must
be on `$PATH`.

## Captured outputs

Raw `tmux capture-pane` output from `check_limit/main.py` is written to
`output/check_limit/{claude,gemini,codex}_output.txt`. These contain
account email, session IDs, and absolute paths, so they're gitignored.

## Tests

Three layers, increasing fidelity and cost — see
[`docs/slack_bridge.md`](./slack_bridge.md#testing) for full details.

```bash
python3 -m unittest tests.test_handlers -v        # ~95 unit tests, <1s
python3 tests/sim_user.py                          # in-process, real tmux + gemini, ~30s
python3 tests/slack_drive.py --cli gemini --skip-roundtrip   # real Slack drive
```

`tests/slack_drive.py` posts as you (via `SLACK_USER_TOKEN` in `.env`)
into a fresh `#<cli>-test_<timestamp>` private channel and walks the full
test plan. Auto-archives at the end. Useful for verifying
Slack-API-layer behavior the in-process sim can't catch.

## Bridge robustness

The bridge survives bridge restarts and socket blips via two recovery
paths (see [Recovery from bridge state loss](./slack_bridge.md#recovery-from-bridge-state-loss)):

- **Auto-relink** — on a free-text message in a channel with no in-memory
  session, the bridge looks up the channel name, matches against the
  named-channel pattern (`<cli>-<slug>-<uniqid>`), and re-attaches to a
  still-live tmux session of the same name. Silent recovery.
- **Socket watchdog** — repeated `Failed to check the state of sock`
  errors trigger `os._exit(1)` so an external supervisor can bring up
  a fresh process. Auto-relink heals state mid-flight.

Use `!debug` in any channel to see the bridge's pid, uptime, in-memory
sessions dict, and any orphan tmux sessions.
