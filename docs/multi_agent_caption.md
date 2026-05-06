# multi_agent_caption

Caption every video clip in a CCTV dataset by extracting the middle frame and
prompting the **Gemini** and **Codex** CLIs in parallel, with quota-aware
fallback across model tiers and full resumability.

## Files

| File | Role |
| --- | --- |
| `src/multi_agent_caption/main.py` | The whole pipeline — frame extraction, two-thread captioning, usage monitor, state, Excel export |

The script also imports `src/check_limit/main.py` for the tmux-based usage
scraping (`capture_cli_usage`, `parse_gemini_rows`, `parse_codex_rows`). See
[check_limit.md](check_limit.md) for that piece.

## Inputs / outputs

- **Input:** a video dataset laid out as
  `<DATASET>/<split>/<class>/*.mp4`, where `<split>` is `train` or `val`
  and `<class>` is one of `falldown · fire · fire_smoke · normal · smoke
  · violence · violence_falldown`. Set the dataset root by exporting
  `MULTI_AGENT_CAPTION_DATASET=/path/to/dataset` (in your shell or
  `.env`); the default is `<repo>/datasets/videos/` so the pipeline
  fails fast with a clear "Input dir not found" if you forget.
- **Outputs (per split, under `output/`):**
  - `captions_<split>.xlsx` — final spreadsheet (one row per video).
  - `captions_<split>/state.jsonl` — append-only event log (resumable).
  - `captions_<split>/frames/<class>/<video_stem>.jpg` — extracted frames (cached, skipped on re-run).
  - `captions_<split>/.codex_last/` — temp dir for codex `--output-last-message` files (snap-sandbox safe path).

The Excel has columns: `split`, `class`, `video`, `frame`, `caption`, `model`, `status`, `error`, `ts`.

## Usage

```bash
# Smoke-test on 4 videos with no live quota scraping
python3 src/multi_agent_caption/main.py --split val --limit 4 --no-usage-monitor

# Full val run (~2.4K videos)
python3 src/multi_agent_caption/main.py --split val

# Full train run (~57K videos) — long-running; let it block on quotas
nohup python3 -u src/multi_agent_caption/main.py --split train > output/captions_train.log 2>&1 &

# Re-export the .xlsx from existing state.jsonl without captioning
python3 src/multi_agent_caption/main.py --split val --export-only
```

### Flags

| Flag | Default | Effect |
| --- | --- | --- |
| `--split {val,train}` | `val` | Which subdir of the dataset to caption |
| `--limit N` | `0` (= all) | Process only the first N items (smoke testing) |
| `--gemini-models` | `gemini-2.5-flash-lite,gemini-2.5-flash,gemini-2.5-pro` | Comma-list, cheap → expensive |
| `--codex-models` | `gpt-5.4,gpt-5.4-mini` | Comma-list (note: ChatGPT-account Codex shares one quota across models) |
| `--gemini-timeout` | `120` | Per-call timeout (s); Pro is slow on hard frames |
| `--codex-timeout` | `240` | Per-call timeout (s) |
| `--no-usage-monitor` | off | Disable the background tmux scraper (workers still try the ladder; they just don't pre-skip blocked buckets) |
| `--export-only` | off | Skip captioning; rebuild the .xlsx from state.jsonl |

## Architecture

### Two workers, opposite ends

```
items[0] ─→ ─→ ─→  front (gemini-first ladder)
                   ...
items[N-1] ←─ ←─ ←  back  (codex-first  ladder)
                   They meet in the middle and stop.
```

Both threads share a single `state.jsonl` and a `claimed` set protected by a
lock so they never double-process an index.

### Model ladders

For each video the worker walks an ordered list of `(provider, model)`
candidates. Defaults:

```
front: gemini:flash-lite → gemini:flash → gemini:pro → codex:gpt-5.4 → codex:mini
back : codex:gpt-5.4    → codex:mini   → gemini:pro → gemini:flash → gemini:flash-lite
```

The back ladder mirrors the front so the two threads naturally hit different
provider buckets first, reducing contention.

### Per-candidate retry policy (`caption_one`)

For every candidate in turn:

1. **Pre-check** the quota cache via `UsageState.candidate_blocked()`. If the
   bucket is ≥ `QUOTA_SKIP_PCT` (95%) full, skip immediately to the next
   candidate.
2. Otherwise try up to **3 attempts**. On each failure:
   - **Rate-limit / transient** (matched against `RATE_LIMIT_HINTS`): sleep
     `5 · 2^attempt` seconds (capped at 60), retry.
   - **Non-retriable**: bail out of this candidate immediately.
3. If all candidates fail → log `status=error` with `error` = last failure.

### Dynamic re-ranking inside a provider block (`reorder_by_quota`)

Within each contiguous same-provider block of the ladder, candidates are
re-sorted at every call by ascending bucket `pct_used`. This means **all three
Gemini tiers are exercised in parallel** instead of draining Flash-Lite to
95% before touching Flash and Pro. Codex models share a single
account-wide bucket pair on a ChatGPT plan, so their order is left alone.

### Block-and-poll when everything is exhausted (`_wait_for_unblocked`)

Earlier versions of the script churned ~10K videos into `error` rows in
minutes once every bucket crossed 95%. The current behavior is:

- Before iterating candidates, check that *at least one* is unblocked.
- If not, poll the cache every 20s (the `usage_monitor` thread refreshes the
  cache every 90s) until a bucket clears or `STOP` fires.

Workers therefore spend their time **waiting for resets** rather than burning
through the queue without producing captions.

### Usage monitor (`usage_monitor` + `UsageState`)

A background thread runs `gemini /model` and `codex /status` (via the
`capture_cli_usage` tmux helper from `check_limit`) every ~90s and updates a
shared `UsageState` cache:

- Gemini buckets: `Flash`, `Flash Lite`, `Pro`.
- Codex buckets: `5h limit`, `Weekly limit` (account-wide on ChatGPT plan).

Each scrape prints a one-line summary into the log:

```
usage: gemini={'Flash': 6, 'Flash Lite': 41, 'Pro': 0}  codex={'5h limit': 36, 'Weekly limit': 23}
```

If `src.check_limit` fails to import, the monitor is silently disabled and
workers operate without pre-skip (they'll still hit and back off on
rate-limit responses from the CLIs themselves).

### State / resumability (`State`)

Every captioning attempt appends one JSON line to `state.jsonl` with status =
`ok | error | frame_error | interrupted`.

- On launch the file is read; **only `ok` rows count as "done"**.
- All other statuses (`error`, `frame_error`, `interrupted`) are re-attempted
  on the next launch.
- `export_excel` keeps the **latest entry per video id**, so a successful
  retry naturally supersedes an earlier `error`.

Frames in `frames/<class>/*.jpg` are cached on disk and reused on resume —
ffmpeg is not re-invoked unless the file is missing or zero-byte.

### Signal handling

`SIGINT` / `SIGTERM` set the `STOP` event. Workers finish whatever CLI call
they're currently waiting on, return early on the next iteration, and `main`
calls `export_excel` before exiting. Safe to `kill -TERM <pid>` at any time.

## Prompt

```
Describe what is visible in this image in 1-2 sentences. Context: this frame
is from a CCTV video clip labeled '<class>'. Focus on observable evidence of
<class>, or note if the labeled event is not yet visible. Be factual and
concise. Reply with the description only, no preamble.
```

The class hint keeps captions grounded in the dataset label, but the prompt
explicitly tells the model to say so when the labeled event isn't visible
(important for clips where the action happens later in the video than the
extracted middle frame).

## Operational notes

- **ChatGPT-plan Codex** shares one quota pair across `gpt-5.4` and
  `gpt-5.4-mini`. Switching between them does not gain new headroom.
  `gpt-5.4-codex` is **not** available on a ChatGPT account.
- **Gemini Pro** is slow — hard frames can exceed the default 120s timeout.
  The retry layer treats timeouts as rate-limits and falls forward through
  the ladder, so Pro timeouts mostly cost a few wasted seconds.
- **Codex 5h** resets every 5 hours, **Codex Weekly** ~weekly. Gemini buckets
  reset roughly daily. The `usage:` log lines show the cached percentages —
  watch for upward creep towards 95%.
- **Throughput** observed:
  - Both providers fresh, Flash-Lite + Codex 5h doing the work: ~880-940/h.
  - Codex 5h exhausted, only Gemini Flash + Pro left: ~500-770/h
    (Pro latency dominates).
  - Everything blocked: 0/h while the monitor waits for the next reset.
- **Kill switch**: `pkill -TERM -f 'multi_agent_caption'` flushes state and
  exports cleanly. Re-launch the same command to resume.

## Smoke test

```bash
python3 src/multi_agent_caption/main.py --split val --limit 4 --no-usage-monitor
```

Expected output:
```
split=val  videos=4  out=.../output/captions_val
front ladder: [...gemini-2.5-flash-lite ... codex:gpt-5.4-mini]
back  ladder: [...codex:gpt-5.4 ... gemini-2.5-flash-lite]
resume: 0 already captioned
[front]     0 falldown             ok     gemini:gemini-2.5-flash-lite ...
[back]      3 falldown             ok     codex:gpt-5.4              ...
...
Done. ok=4 err=0 skip=0  exported 4 rows -> .../output/captions_val.xlsx
```
