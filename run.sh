#!/usr/bin/env bash
# Install the project and run the scoring backend (Mac / Linux).
#
#   ./run.sh --ui                            start the JavaScript UI + FastAPI at http://127.0.0.1:8000
#   ./run.sh --ui --no-install               start offline using packages already in .venv
#   PORT=8001 ./run.sh --ui                  choose a different local port
#   ./run.sh --dry-run                       offline try-out: fake LLM + synthetic data, no keys needed
#   ./run.sh --symbols SBIN HDFCBANK RBLBANK  real run on chosen stocks
#   ./run.sh --sub-sector bank               all banks
#   ./run.sh --yes                           real run on the whole financial universe (98 stocks)
#   ./run.sh --setup-only                    install + tests only
# Anything else is passed straight to `python -m app.main` (see: python -m app.main --help).
# Extra flags: --skip-tests, --reinstall, --no-install, --port=8001, -h/--help
set -euo pipefail
cd "$(dirname "$0")"

DRY=0; SETUP_ONLY=0; SKIP_TESTS=0; REINSTALL=0; NO_INSTALL=0; YES=0; API=0; PORT="${PORT:-8000}"; ARGS=()
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    --api|--ui) API=1 ;;
    --no-install) NO_INSTALL=1 ;;
    --port=*) PORT="${a#*=}" ;;
    --setup-only) SETUP_ONLY=1 ;;
    --skip-tests) SKIP_TESTS=1 ;;
    --reinstall) REINSTALL=1 ;;
    --yes|-y) YES=1 ;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) ARGS+=("$a") ;;
  esac
done

say() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

# 1. Python >= 3.11 ---------------------------------------------------------------------------
PY=""
for c in python3.13 python3.12 python3.11 python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
[ -n "$PY" ] || die "Python 3.11 or newer is required. Install it (e.g. 'brew install python@3.12' or 'sudo apt install python3.12 python3.12-venv') and re-run."
say "Using $($PY --version) ($(command -v $PY))"

# 2. Virtualenv + dependencies ----------------------------------------------------------------
[ "$REINSTALL" -eq 1 ] && rm -rf .venv
if [ ! -d .venv ]; then
  say "Creating .venv"
  "$PY" -m venv .venv || die "Could not create a virtualenv. On Debian/Ubuntu: sudo apt install python3-venv"
fi
# shellcheck disable=SC1091
source .venv/bin/activate
STAMP=.venv/.requirements.stamp
if [ "$NO_INSTALL" -eq 1 ]; then
  say "Skipping dependency installation (--no-install)"
  python -c 'import fastapi, uvicorn' 2>/dev/null || die "FastAPI/Uvicorn are missing from .venv. Restore network and run ./run.sh --ui, or install dependencies manually."
elif [ ! -f "$STAMP" ] || [ requirements.txt -nt "$STAMP" ]; then
  say "Installing dependencies"
  if python -m pip install --quiet --upgrade pip && python -m pip install --quiet -r requirements.txt; then
    touch "$STAMP"
  else
    if [ "$API" -eq 1 ] && python -c 'import fastapi, uvicorn' 2>/dev/null; then
      say "Dependency installation failed; FastAPI/Uvicorn are available, continuing without updating dependencies"
      say "If a pipeline endpoint fails due to a missing package, restore network and run ./run.sh --setup-only"
    else
      die "Dependency installation failed. Check internet/DNS, or use --no-install if the required packages are already installed."
    fi
  fi
else
  say "Dependencies already installed (use --reinstall to redo)"
fi

# 3. .env -------------------------------------------------------------------------------------
if [ ! -f .env ]; then
  cp .env.example .env
  say "Created .env from .env.example"
  if [ "$DRY" -eq 0 ] && [ "$SETUP_ONLY" -eq 0 ]; then
    cat <<'MSG'

Fill in .env before a real run:
  LLM_PROVIDER and the matching key (OPENAI_API_KEY / OPENROUTER_API_KEY / ANTHROPIC_API_KEY ...), or OLLAMA_BASE_URL for a local model
  FYERS_APP_ID and FYERS_ACCESS_TOKEN (the token expires daily)
  FRED_API_KEY (free)
Choose the model per agent in app/service/models/model_config.json, then run ./run.sh again.
(To just try the app now: ./run.sh --dry-run)
MSG
    exit 0
  fi
fi

# 4. Offline tests ----------------------------------------------------------------------------
if [ "$SKIP_TESTS" -eq 0 ]; then
  say "Running offline tests"
  python -m pytest -q || die "Tests failed. Fix them or re-run with --skip-tests."
fi
[ "$SETUP_ONLY" -eq 1 ] && { say "Setup complete."; exit 0; }

# 5. Start the backend ------------------------------------------------------------------------
if [ "$API" -eq 1 ]; then
  [[ "$PORT" =~ ^[0-9]+$ ]] || die "PORT must be a numeric port (e.g. --port=8001)"
  [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "PORT must be between 1 and 65535"
  say "Starting Stock Scoring UI at http://127.0.0.1:${PORT}"
  say "Interactive API docs: http://127.0.0.1:${PORT}/docs"
  exec python -m uvicorn app.api:app --host 127.0.0.1 --port "$PORT" --no-access-log
fi

if [ "$DRY" -eq 1 ]; then
  say "Dry run (fake LLM, synthetic data)"
  exec python -m scripts.dry_run
fi

if [ "${#ARGS[@]}" -eq 0 ] && [ "$YES" -eq 0 ]; then
  if [ -t 0 ]; then
    read -r -p "No stocks given: run the WHOLE universe (98 stocks, many LLM calls)? [y/N] " ans
    case "$ans" in y|Y|yes|YES) ;; *) echo "Cancelled. Example: ./run.sh --symbols SBIN HDFCBANK"; exit 0 ;; esac
  else
    die "No stocks given. Pass --symbols ..., --sub-sector ..., or --yes for the whole universe."
  fi
fi

say "Starting scoring backend (it launches the shared MCP tool server itself)"
exec python -m app.main "${ARGS[@]}"
