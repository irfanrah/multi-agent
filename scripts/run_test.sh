#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

RUN_GEMINI=1
RUN_CODEX=1
RUN_SLACK=0
RUN_UPLOAD=0
SKIP_ROUNDTRIP=1
KEEP_CHANNELS=0

usage() {
  cat <<'EOF'
Usage: scripts/run_test.sh [options]

Runs the repo's current test surface from one entrypoint:
  - unit tests
  - real-CLI integration tests
  - in-process sim_user flow
  - optional live Slack drive tests

Options:
  --gemini-only        Run only Gemini-related flows
  --codex-only         Run only Codex-related flows
  --with-slack         Include tests/slack_drive.py
  --with-upload        Include tests/slack_drive_upload.py (implies --with-slack)
  --roundtrip          Let Slack drive run the real agent round-trip step
  --keep               Pass --keep to live Slack tests
  -h, --help           Show this help
EOF
}

run_step() {
  local label="$1"
  shift

  printf '\n==> %s\n' "$label"
  (
    cd "$ROOT_DIR"
    "$@"
  )
}

need_cli() {
  command -v "$1" >/dev/null 2>&1
}

need_python_module() {
  python3 -c "import $1" >/dev/null 2>&1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --gemini-only)
      RUN_GEMINI=1
      RUN_CODEX=0
      ;;
    --codex-only)
      RUN_GEMINI=0
      RUN_CODEX=1
      ;;
    --with-slack)
      RUN_SLACK=1
      ;;
    --with-upload)
      RUN_SLACK=1
      RUN_UPLOAD=1
      ;;
    --roundtrip)
      SKIP_ROUNDTRIP=0
      ;;
    --keep)
      KEEP_CHANNELS=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
  shift
done

if ! need_python_module slack_bolt; then
  echo "Missing Python dependency: slack_bolt" >&2
  echo "Install project deps first: pip install -r requirements.txt" >&2
  exit 1
fi

run_step "Unit tests" python3 -m unittest tests.test_handlers -v

if (( RUN_GEMINI && RUN_CODEX )); then
  run_step "Integration tests (all CLIs)" python3 -m unittest tests.test_integration -v
else
  suites=()
  if (( RUN_GEMINI )); then
    suites+=("tests.test_integration.GeminiSmokeTests")
  fi
  if (( RUN_CODEX )); then
    suites+=("tests.test_integration.CodexSmokeTests")
  fi
  if (( ${#suites[@]} > 0 )); then
    run_step "Integration tests (selected CLIs)" python3 -m unittest "${suites[@]}" -v
  fi
fi

if (( RUN_GEMINI )); then
  run_step "Sim user flow (gemini)" python3 tests/sim_user.py
fi

if (( RUN_SLACK )); then
  slack_args=()
  if (( SKIP_ROUNDTRIP )); then
    slack_args+=(--skip-roundtrip)
  fi
  if (( KEEP_CHANNELS )); then
    slack_args+=(--keep)
  fi

  if (( RUN_GEMINI )); then
    run_step "Live Slack drive (gemini)" python3 tests/slack_drive.py --cli gemini "${slack_args[@]}"
  fi
  if (( RUN_CODEX )); then
    run_step "Live Slack drive (codex)" python3 tests/slack_drive.py --cli codex "${slack_args[@]}"
  fi

  if (( RUN_UPLOAD )); then
    upload_args=()
    if (( KEEP_CHANNELS )); then
      upload_args+=(--keep)
    fi
    if (( RUN_GEMINI )); then
      run_step "Live upload flow (gemini)" python3 tests/slack_drive_upload.py --cli gemini "${upload_args[@]}"
    fi
    if (( RUN_CODEX )); then
      run_step "Live upload flow (codex)" python3 tests/slack_drive_upload.py --cli codex "${upload_args[@]}"
    fi
  fi
fi

if (( RUN_GEMINI )) && ! need_cli gemini; then
  printf '\nNote: gemini is not on PATH; Gemini integration tests rely on unittest skips.\n'
fi

if (( RUN_CODEX )) && ! need_cli codex; then
  printf '\nNote: codex is not on PATH; Codex integration tests rely on unittest skips.\n'
fi

printf '\nAll requested test commands completed.\n'
