"""Caption videos in train_val_master_v2/<split> using Gemini and Codex CLIs.

Two worker threads consume a sorted video list from opposite ends:
- the *front* thread iterates index 0 -> N-1 with a Gemini-first ladder
- the *back*  thread iterates index N-1 -> 0 with a Codex-first  ladder

Each worker has an ordered list of (provider, model) candidates. For every
video it tries each candidate in turn (with 3 retries + exponential backoff on
rate-limit / transient errors), and on persistent failure advances to the next
candidate. A background usage-monitor scrapes each CLI's built-in usage panel
(`gemini /model`, `codex /status` via tmux capture) every ~90s and skips
candidates whose quota bucket is >= QUOTA_SKIP_PCT used. When *every* candidate
is blocked, workers block-and-poll instead of churning videos into errors.

Within the Gemini section of each ladder, candidates are dynamically reordered
at every call by ascending bucket fill so Flash / Flash-Lite / Pro are all
exercised in parallel rather than drained sequentially.

Results stream into state.jsonl (atomic append, file-locked, resumable). At
the end (or on Ctrl-C / SIGTERM) the script writes captions_<split>.xlsx.

Run from project root:
    python3 src/multi_agent_caption/main.py --split val
    python3 src/multi_agent_caption/main.py --split train --limit 100
    python3 src/multi_agent_caption/main.py --split val --export-only
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# --- check_limit integration -------------------------------------------------
# main.py lives at src/multi_agent_caption/main.py, so ../.. = src/
_SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _SRC_DIR)
try:
    from check_limit.main import (  # noqa: E402
        capture_cli_usage, parse_gemini_rows, parse_codex_rows,
    )
    HAS_LIMIT_CHECK = True
except Exception:
    HAS_LIMIT_CHECK = False

# Project layout
_REPO_ROOT = os.path.dirname(_SRC_DIR)
DATASET = Path("/mnt/nas192/Research_materials/Kur/PIA_clip_dataset/train_val_master_v2")
OUT_ROOT = Path(_REPO_ROOT) / "output"
CLASSES = ["falldown", "fire", "fire_smoke", "normal", "smoke", "violence", "violence_falldown"]

PROMPT_TMPL = (
    "Describe what is visible in this image in 1-2 sentences. "
    "Context: this frame is from a CCTV video clip labeled '{klass}'. "
    "Focus on observable evidence of {klass}, or note if the labeled event is not yet visible. "
    "Be factual and concise. Reply with the description only, no preamble."
)

# Substrings indicating "transient — back off and retry" rather than a hard error.
RATE_LIMIT_HINTS = [
    "rate limit", "rate-limit", "rate_limit",
    "quota", "429", "too many requests", "exhausted",
    "throttle", "resource_exhausted", "overloaded",
    "service unavailable", "503", "504",
    "deadline exceeded",
]

# Map a Gemini model id -> the bucket label that appears in `gemini /model`.
GEMINI_BUCKET = {
    "gemini-2.5-flash-lite": "Flash Lite",
    "gemini-2.5-flash":      "Flash",
    "gemini-2.5-pro":        "Pro",
    "gemini-3-flash":        "Flash",
    "gemini-3.1-pro":        "Pro",
}
# Codex /status reports a single account-wide pair (5h + Weekly) shared by
# every codex model variant on a ChatGPT account.
CODEX_BUCKETS = ("5h limit", "Weekly limit")
QUOTA_SKIP_PCT = 95  # skip a candidate if its bucket is >= this %


# ---------- logging ----------------------------------------------------------

PRINT_LOCK = threading.Lock()

def log(msg: str) -> None:
    with PRINT_LOCK:
        sys.stdout.write(msg.rstrip() + "\n")
        sys.stdout.flush()


# ---------- types ------------------------------------------------------------

@dataclass(frozen=True)
class Item:
    idx: int
    split: str
    klass: str
    video: Path

    @property
    def vid(self) -> str:
        return f"{self.split}/{self.klass}/{self.video.name}"


@dataclass(frozen=True)
class Candidate:
    provider: str   # "gemini" | "codex"
    model: str
    timeout: int

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"


# ---------- dataset / frame extraction ---------------------------------------

def collect(split: str) -> list[Item]:
    items: list[Item] = []
    i = 0
    for c in CLASSES:
        d = DATASET / split / c
        if not d.is_dir():
            continue
        for vp in sorted(d.glob("*.mp4")):
            items.append(Item(i, split, c, vp))
            i += 1
    return items


def video_duration(vp: Path) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(vp)],
            capture_output=True, text=True, timeout=20,
        )
        return float(r.stdout.strip())
    except Exception:
        return 1.0


def extract_frame(vp: Path, frame_path: Path) -> bool:
    if frame_path.exists() and frame_path.stat().st_size > 0:
        return True
    frame_path.parent.mkdir(parents=True, exist_ok=True)
    dur = video_duration(vp)
    mid = max(0.05, dur / 2.0)
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-ss", f"{mid:.3f}", "-i", str(vp),
             "-frames:v", "1", "-q:v", "3", str(frame_path)],
            capture_output=True, timeout=60,
        )
    except Exception:
        return False
    return frame_path.exists() and frame_path.stat().st_size > 0


def is_rate_limit(text: str) -> bool:
    t = (text or "").lower()
    return any(h in t for h in RATE_LIMIT_HINTS)


# ---------- model runners ----------------------------------------------------

# The codex CLI runs inside a snap sandbox that disallows /tmp writes for the
# `--output-last-message` file. We point it at a path inside the project's
# output dir, configured per-split in main().
_CODEX_LAST_DIR: Optional[Path] = None

def _set_codex_last_dir(path: Path) -> None:
    global _CODEX_LAST_DIR
    _CODEX_LAST_DIR = path
    path.mkdir(parents=True, exist_ok=True)


def run_gemini(model: str, frame: Path, klass: str, timeout: int) -> tuple[Optional[str], Optional[str]]:
    prompt = f"@{frame} {PROMPT_TMPL.format(klass=klass)}"
    try:
        p = subprocess.run(
            ["gemini", "--skip-trust", "-m", model, "-o", "text", "-p", prompt],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, "timeout"
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    cleaned_lines = []
    for ln in out.splitlines():
        if ln.startswith("Ripgrep is not available"):
            continue
        if ln.startswith("Loaded cached credentials"):
            continue
        cleaned_lines.append(ln)
    cleaned = " ".join(l.strip() for l in cleaned_lines if l.strip())
    # Gemini sometimes emits 429 events on stderr but still returns a usable
    # response — only fail if no text came back.
    if cleaned:
        return cleaned, None
    if p.returncode != 0:
        return None, (err[:400] or out[:400] or f"rc={p.returncode}")
    return None, (err[:400] or "empty output")


def run_codex(model: str, frame: Path, klass: str, timeout: int) -> tuple[Optional[str], Optional[str]]:
    assert _CODEX_LAST_DIR is not None, "call _set_codex_last_dir() before run_codex"
    last = _CODEX_LAST_DIR / f"{os.getpid()}_{threading.get_ident()}_{int(time.time()*1000)}.txt"
    try:
        p = subprocess.run(
            ["codex", "exec", "--skip-git-repo-check",
             "-m", model,
             "--output-last-message", str(last),
             "-i", str(frame), "--",
             PROMPT_TMPL.format(klass=klass)],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        if last.exists():
            try: last.unlink()
            except Exception: pass
        return None, "timeout"
    err = (p.stderr or "").strip()
    out = (p.stdout or "").strip()
    text = ""
    if last.exists():
        try:
            text = last.read_text().strip()
        finally:
            try: last.unlink()
            except Exception: pass
    if not text:
        # Fallback: parse the trailing repeat after the "tokens used / N" footer.
        m = re.search(r"tokens used\s*\n[\d,]+\s*\n(.+)\Z", out, re.S)
        if m:
            text = m.group(1).strip()
    if text:
        return text, None
    return None, (err[:400] or out[:400] or f"rc={p.returncode}")


def run_candidate(c: Candidate, frame: Path, klass: str) -> tuple[Optional[str], Optional[str]]:
    if c.provider == "gemini":
        return run_gemini(c.model, frame, klass, c.timeout)
    if c.provider == "codex":
        return run_codex(c.model, frame, klass, c.timeout)
    return None, f"unknown provider {c.provider}"


# ---------- usage monitor ----------------------------------------------------

class UsageState:
    """Background scrape of `gemini /model` and `codex /status`. Thread-safe.

    Workers consult `candidate_blocked()` before issuing a call, and the same
    cache feeds `reorder_by_quota()`.
    """
    def __init__(self):
        self.lock = threading.Lock()
        self.gemini: dict[str, int] = {}      # bucket label -> pct used
        self.codex: dict[str, int] = {}       # bucket label -> pct used
        self.last_refresh = 0.0

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "gemini": dict(self.gemini),
                "codex": dict(self.codex),
                "age_s": int(time.time() - self.last_refresh) if self.last_refresh else None,
            }

    def candidate_blocked(self, c: Candidate) -> Optional[str]:
        """Return reason if the candidate's bucket is over QUOTA_SKIP_PCT, else None."""
        with self.lock:
            if c.provider == "gemini":
                bucket = GEMINI_BUCKET.get(c.model)
                pct = self.gemini.get(bucket) if bucket else None
                if pct is not None and pct >= QUOTA_SKIP_PCT:
                    return f"gemini bucket '{bucket}' at {pct}%"
            elif c.provider == "codex":
                # Either codex bucket >= threshold blocks all codex models.
                for b in CODEX_BUCKETS:
                    pct = self.codex.get(b)
                    if pct is not None and pct >= QUOTA_SKIP_PCT:
                        return f"codex bucket '{b}' at {pct}%"
        return None


def _scrape_gemini(usage: UsageState, max_wait: int = 30) -> None:
    raw = capture_cli_usage(
        "gemini /model", "capvid_gemini",
        ready_check=lambda t: len(parse_gemini_rows(t)) >= 1,
        max_wait=max_wait,
    )
    rows = parse_gemini_rows(raw)
    with usage.lock:
        for r in rows:
            usage.gemini[r["label"]] = r["pct_used"]


def _scrape_codex(usage: UsageState, max_wait: int = 35) -> None:
    raw = capture_cli_usage(
        "codex", "capvid_codex",
        send_keys="/status",
        prompt_ready=lambda t: "›" in t,
        ready_check=lambda t: len(parse_codex_rows(t)) >= 1,
        max_wait=max_wait,
    )
    rows = parse_codex_rows(raw)
    with usage.lock:
        for r in rows:
            usage.codex[r["label"]] = r["pct_used"]


def usage_monitor(usage: UsageState, stop: threading.Event, period_s: int = 90) -> None:
    if not HAS_LIMIT_CHECK:
        log("usage_monitor: src.check_limit not importable; skipping")
        return
    while not stop.is_set():
        for label, fn in [("gemini", _scrape_gemini), ("codex", _scrape_codex)]:
            try:
                fn(usage)
            except Exception as e:
                log(f"usage_monitor: {label} scrape failed: {e}")
        with usage.lock:
            usage.last_refresh = time.time()
        snap = usage.snapshot()
        log(f"usage: gemini={snap['gemini']}  codex={snap['codex']}")
        for _ in range(period_s):
            if stop.is_set(): return
            time.sleep(1)


# ---------- state ------------------------------------------------------------

class State:
    """Append-only JSONL store, file-locked for cross-thread safety.

    Resumable: only rows with status=="ok" populate `done`. Error rows from a
    prior run will be re-attempted on the next launch (and the Excel exporter
    keeps the latest entry per id, so a successful retry supersedes the error).
    """
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.done: dict[str, dict] = {}
        self.claimed: set[int] = set()
        if path.exists():
            with path.open() as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if obj.get("status") == "ok":
                        self.done[obj["id"]] = obj

    def already_ok(self, vid: str) -> bool:
        return vid in self.done

    def claim(self, idx: int) -> bool:
        with self.lock:
            if idx in self.claimed:
                return False
            self.claimed.add(idx)
            return True

    def release(self, idx: int) -> None:
        with self.lock:
            self.claimed.discard(idx)

    def append(self, obj: dict) -> None:
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        with self.lock:
            with self.path.open("a") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    f.write(line)
                    f.flush()
                    os.fsync(f.fileno())
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            if obj.get("status") == "ok":
                self.done[obj["id"]] = obj


# ---------- worker -----------------------------------------------------------

STOP = threading.Event()
COUNTERS = {"ok": 0, "err": 0, "skip": 0}
COUNTERS_LOCK = threading.Lock()


def reorder_by_quota(candidates: list[Candidate], usage: UsageState) -> list[Candidate]:
    """Within each contiguous same-provider block, sort by ascending bucket
    fill so all tiers see traffic in parallel rather than draining one before
    the next. Codex models share one bucket pair so their order is preserved.
    """
    out: list[Candidate] = []
    i = 0
    while i < len(candidates):
        j = i
        while j < len(candidates) and candidates[j].provider == candidates[i].provider:
            j += 1
        block = candidates[i:j]
        if candidates[i].provider == "gemini":
            def key(c: Candidate) -> int:
                bucket = GEMINI_BUCKET.get(c.model)
                with usage.lock:
                    pct = usage.gemini.get(bucket) if bucket else None
                return pct if pct is not None else 0
            block = sorted(block, key=key)
        out.extend(block)
        i = j
    return out


def _wait_for_unblocked(candidates: list[Candidate], usage: UsageState,
                        worker_name: str, vid: str) -> bool:
    """Block until at least one candidate's bucket falls below QUOTA_SKIP_PCT,
    or STOP fires. Returns True if a candidate is available, False if interrupted."""
    if any(usage.candidate_blocked(c) is None for c in candidates):
        return True
    log(f"[{worker_name}] all candidates blocked for {vid}; waiting for quota refresh")
    waited = 0
    while not STOP.is_set():
        # poll cache every 20s; usage_monitor refreshes the cache every 90s.
        if STOP.wait(20):
            return False
        waited += 20
        if any(usage.candidate_blocked(c) is None for c in candidates):
            log(f"[{worker_name}] quota cleared after {waited}s; resuming")
            return True
    return False


def _base(item: Item, frame_p: Path) -> dict:
    return {
        "id": item.vid, "idx": item.idx,
        "split": item.split, "class": item.klass,
        "video": str(item.video), "frame": str(frame_p),
        "caption": None, "model": None,
        "status": None, "error": None,
        "ts": int(time.time()),
    }


def caption_one(item: Item, frames_root: Path, candidates: list[Candidate],
                usage: UsageState, worker_name: str = "?") -> dict:
    frame_p = frames_root / item.klass / (item.video.stem + ".jpg")
    if not extract_frame(item.video, frame_p):
        return {**_base(item, frame_p), "status": "frame_error", "error": "ffmpeg failed"}

    # Block when *every* candidate's quota is exhausted, instead of immediately
    # writing an error and burning through the queue.
    if not _wait_for_unblocked(candidates, usage, worker_name, item.vid):
        return {**_base(item, frame_p), "status": "interrupted"}

    ordered = reorder_by_quota(candidates, usage)

    last_err = None
    for c in ordered:
        if STOP.is_set():
            return {**_base(item, frame_p), "status": "interrupted"}
        block = usage.candidate_blocked(c)
        if block:
            last_err = f"{c.label}: skipped ({block})"
            continue
        for attempt in range(3):
            if STOP.is_set():
                return {**_base(item, frame_p), "status": "interrupted"}
            text, err = run_candidate(c, frame_p, item.klass)
            if text:
                return {
                    **_base(item, frame_p),
                    "caption": text, "model": c.label,
                    "status": "ok",
                }
            last_err = f"{c.label}: {err}"
            if is_rate_limit(err or ""):
                wait = min(60, 5 * (2 ** attempt))
                log(f"  [{c.label}] rate-limit on {item.vid}, sleep {wait}s ({(err or '')[:120]})")
                if STOP.wait(wait):
                    break
            else:
                log(f"  [{c.label}] non-retriable on {item.vid}: {(err or '')[:160]}")
                break

    return {**_base(item, frame_p), "status": "error", "error": last_err}


def worker(name: str, items: list[Item], state: State, frames_root: Path,
           order: str, candidates: list[Candidate], usage: UsageState,
           total: int) -> None:
    if order == "front":
        seq: range = range(0, len(items))
    else:
        seq = range(len(items) - 1, -1, -1)

    for idx in seq:
        if STOP.is_set():
            return
        item = items[idx]
        if state.already_ok(item.vid):
            with COUNTERS_LOCK: COUNTERS["skip"] += 1
            continue
        if not state.claim(idx):
            continue
        try:
            res = caption_one(item, frames_root, candidates, usage, name)
            state.append(res)
            with COUNTERS_LOCK:
                if res["status"] == "ok": COUNTERS["ok"] += 1
                elif res["status"] != "interrupted": COUNTERS["err"] += 1
            done = COUNTERS["ok"] + COUNTERS["err"]
            log(f"[{name}] {idx:5d} {item.klass:20s} {res['status']:6s} "
                f"{(res.get('model') or '-'):26s} "
                f"{(res.get('caption') or res.get('error') or '')[:70]}  "
                f"({done}/{total} ok={COUNTERS['ok']} err={COUNTERS['err']})")
        finally:
            state.release(idx)


# ---------- excel export -----------------------------------------------------

def export_excel(state_path: Path, xlsx_path: Path) -> int:
    """Build an .xlsx from state.jsonl. When a video has multiple rows (e.g. a
    prior `error` superseded by a later `ok`), the latest entry wins.
    """
    import pandas as pd
    rows: dict[str, dict] = {}
    if not state_path.exists():
        return 0
    with state_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            rows[obj["id"]] = obj
    if not rows:
        return 0
    df = pd.DataFrame(rows.values())
    df = df.sort_values(["split", "class", "video"]).reset_index(drop=True)
    cols = ["split", "class", "video", "frame", "caption", "model", "status", "error", "ts"]
    df = df[[c for c in cols if c in df.columns]]
    xlsx_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_excel(xlsx_path, index=False)
    return len(df)


# ---------- main -------------------------------------------------------------

DEFAULT_GEMINI_LADDER = "gemini-2.5-flash-lite,gemini-2.5-flash,gemini-2.5-pro"
DEFAULT_CODEX_LADDER = "gpt-5.4,gpt-5.4-mini"


def build_candidates(provider_order: list[str], gemini_models: list[str],
                     codex_models: list[str], gem_to: int, cx_to: int) -> list[Candidate]:
    out: list[Candidate] = []
    for prov in provider_order:
        if prov == "gemini":
            out.extend(Candidate("gemini", m, gem_to) for m in gemini_models)
        elif prov == "codex":
            out.extend(Candidate("codex", m, cx_to) for m in codex_models)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Caption CCTV clips by extracting middle frames and prompting "
                    "Gemini/Codex CLIs in parallel with quota-aware fallback."
    )
    ap.add_argument("--split", default="val", choices=["val", "train"])
    ap.add_argument("--limit", type=int, default=0,
                    help="Process only the first N items (0 = all). Useful for smoke tests.")
    ap.add_argument("--gemini-models", default=DEFAULT_GEMINI_LADDER,
                    help="Comma-separated Gemini model ids (cheap -> expensive).")
    ap.add_argument("--codex-models", default=DEFAULT_CODEX_LADDER,
                    help="Comma-separated Codex model ids (default -> mini).")
    ap.add_argument("--gemini-timeout", type=int, default=120)
    ap.add_argument("--codex-timeout", type=int, default=240)
    ap.add_argument("--no-usage-monitor", action="store_true",
                    help="Disable the background tmux scraper.")
    ap.add_argument("--export-only", action="store_true",
                    help="Skip captioning; just rebuild the .xlsx from state.jsonl.")
    args = ap.parse_args()

    out_dir = OUT_ROOT / f"captions_{args.split}"
    out_dir.mkdir(parents=True, exist_ok=True)
    frames_root = out_dir / "frames"
    state_path = out_dir / "state.jsonl"
    xlsx_path = OUT_ROOT / f"captions_{args.split}.xlsx"
    _set_codex_last_dir(out_dir / ".codex_last")

    if args.export_only:
        n = export_excel(state_path, xlsx_path)
        log(f"Exported {n} rows -> {xlsx_path}")
        return 0

    items = collect(args.split)
    if args.limit:
        items = items[: args.limit]
    log(f"split={args.split}  videos={len(items)}  out={out_dir}")

    gem_models = [m.strip() for m in args.gemini_models.split(",") if m.strip()]
    cx_models  = [m.strip() for m in args.codex_models.split(",") if m.strip()]
    front_cands = build_candidates(["gemini", "codex"], gem_models, cx_models,
                                   args.gemini_timeout, args.codex_timeout)
    back_cands  = build_candidates(["codex", "gemini"], list(reversed(gem_models)),
                                   cx_models, args.gemini_timeout, args.codex_timeout)
    log(f"front ladder: {[c.label for c in front_cands]}")
    log(f"back  ladder: {[c.label for c in back_cands]}")

    state = State(state_path)
    log(f"resume: {len(state.done)} already captioned")

    usage = UsageState()
    if not args.no_usage_monitor and HAS_LIMIT_CHECK:
        threading.Thread(target=usage_monitor, args=(usage, STOP, 90),
                         name="usage", daemon=True).start()

    def handle_sig(sig, frame):
        log("\nReceived signal, flushing state and exporting Excel...")
        STOP.set()
    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    threads = [
        threading.Thread(
            target=worker, name="front",
            args=("front", items, state, frames_root, "front", front_cands, usage, len(items)),
            daemon=True),
        threading.Thread(
            target=worker, name="back",
            args=("back", items, state, frames_root, "back", back_cands, usage, len(items)),
            daemon=True),
    ]
    for t in threads: t.start()
    try:
        for t in threads:
            while t.is_alive():
                t.join(timeout=1.0)
    except KeyboardInterrupt:
        STOP.set()
        for t in threads:
            t.join(timeout=10.0)

    n = export_excel(state_path, xlsx_path)
    log(f"\nDone. ok={COUNTERS['ok']} err={COUNTERS['err']} skip={COUNTERS['skip']}  exported {n} rows -> {xlsx_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
