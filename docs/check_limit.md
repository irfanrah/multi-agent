# check_limit

Reads quota / rate-limit info from three AI CLIs by running each one in a
disposable `tmux` session, screen-scraping the rendered panel, and parsing
out percentages and reset times.

## Files

| File | Role |
| --- | --- |
| `src/check_limit/main.py` | tmux capture helper, three parsers, CLI entry point that prints a one-line summary per CLI |
| `src/check_limit/widget.py` | Tk desktop widget that imports the helpers and renders bars; auto-refreshes every 60s |

## Capture helper — `capture_cli_usage`

The single primitive everything else builds on (`main.py:11`). Two modes:

- **Legacy / fixed sleeps** — start session, sleep `startup_wait`,
  optionally type `send_keys` + Enter, sleep `post_wait`, capture once.
  Used by the script's `__main__`.
- **Polling** — caller passes `ready_check(text) -> bool`; we re-capture
  every `poll` seconds until ready or `max_wait`. If `prompt_ready` is
  also given, we wait for that to be true before sending keys (handy for
  TUIs like Codex that need a moment before accepting input). The widget
  uses this mode so it doesn't return on a half-rendered panel.

The session is always killed in a `finally` block. Each caller picks a
distinct session name so multiple captures can run in parallel.

## Per-CLI capture commands

| CLI | Command | Notes |
| --- | --- | --- |
| Claude | `claude /usage` | Slash command works at launch — no `send_keys` needed |
| Gemini | `gemini /model` | Same |
| Codex  | `codex` then send `/status` | Codex slash commands only work *interactively*, so we launch the REPL and type the command. `prompt_ready` is `"›" in text` |

## Parsers

Each parser strips box-drawing characters (`BOX_CHARS`), collapses
whitespace, and matches CLI-specific row formats:

- `parse_claude_rows` — three labels (`Current session`, `Current week
  (all models)`, `Current week (Sonnet only)`), each followed by a bar +
  `N% used`, optionally followed by `Resets ...`.
- `parse_gemini_rows` — `Flash | Flash Lite | Pro` followed by `N%` and
  an optional `Resets: <time>`.
- `parse_codex_rows` — `<thing> limit: ... N% used|left (resets ...)`.
  Has a fallback that reads the footer line `gpt-X default · 100% left ·
  /path` when the panel form isn't found.

All three return `[{"label", "pct_used", "reset"}, ...]`. The CLI
entrypoint joins these with `format_rows()` into a single line per CLI
and writes the raw capture to `output/check_limit/<cli>_output.txt`.

## Tk widget

`widget.py` reuses the same parsers. For each CLI it sets `expected`
(minimum row count it should see when fully rendered) and hands the
parser to `capture_cli_usage` as the `ready_check` — i.e. "stop polling
once the parser finds at least N rows". The three captures run on
threads with separate tmux session names, then `_on_results` redraws the
window on the Tk main thread and schedules another refresh in 60s.

The color ramp (`color_for`) is green/yellow/orange/red at 50/75/90%
thresholds. Palette is Catppuccin Mocha.

## Failure modes worth knowing

- **Snap-installed CLIs are slow to launch.** `MAX_WAIT_SEC = 45` in the
  widget; the bridge's similar fetch uses 30s with a sequential retry
  pass. Below that, polling will return whatever it got.
- **Codex `›` heuristic.** `prompt_ready` checks for the input-prompt
  glyph. If Codex changes its prompt character, `prompt_ready` will
  never fire and the call falls back on the `max_wait` timeout.
- **Box-character drift.** Parsers strip a fixed `BOX_CHARS` class. If a
  CLI starts using a glyph that isn't in that class, the row regex may
  miss matches; add the new glyph to `BOX_CHARS`.
