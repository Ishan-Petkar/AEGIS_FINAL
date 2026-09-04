#!/usr/bin/env bash
# scripts/dev-up.sh — start the entire AEGIS Operations Console stack:
# Postgres, the FastAPI backend (uvicorn), the Next.js console, and
# (by default) a live replay of friday-morning traffic so the console
# isn't sitting idle when it opens.
#
# Cross-platform: works on Linux, macOS, and Windows (Git Bash / MSYS2 / WSL).
# On native Windows PowerShell, you can also use: scripts\dev-up.ps1
#
# Usage:
#   scripts/dev-up.sh                 # start everything, start replay
#   scripts/dev-up.sh --no-replay     # start everything, leave replay idle
#   scripts/dev-up.sh --restart       # stop-then-start backend/frontend
#   scripts/dev-up.sh --dataset wednesday --speed 10   # non-default replay
#
# Logs and PID files live in .dev/ (gitignored) at the repo root.
# Stop everything with scripts/dev-down.sh.

set -uo pipefail

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$REPO_ROOT"

DEV_DIR="$REPO_ROOT/.dev"
mkdir -p "$DEV_DIR"

BACKEND_PID_FILE="$DEV_DIR/backend.pid"
FRONTEND_PID_FILE="$DEV_DIR/frontend.pid"
BACKEND_LOG="$DEV_DIR/backend.log"
FRONTEND_LOG="$DEV_DIR/frontend.log"

BACKEND_HOST="127.0.0.1"
BACKEND_PORT="8000"
FRONTEND_PORT="3000"
BACKEND_URL="http://${BACKEND_HOST}:${BACKEND_PORT}"
FRONTEND_URL="http://127.0.0.1:${FRONTEND_PORT}"

START_REPLAY=1
RESTART=0
REPLAY_DATASET="friday-morning"
REPLAY_SPEED="20.0"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-replay) START_REPLAY=0; shift ;;
    --restart) RESTART=1; shift ;;
    --dataset) REPLAY_DATASET="$2"; shift 2 ;;
    --speed) REPLAY_SPEED="$2"; shift 2 ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

# Colors (no-op if not a real terminal)
if [[ -t 1 ]]; then
  BOLD="\033[1m"; DIM="\033[2m"; GREEN="\033[32m"; YELLOW="\033[33m"; RED="\033[31m"; RESET="\033[0m"
else
  BOLD=""; DIM=""; GREEN=""; YELLOW=""; RED=""; RESET=""
fi

ok()   { echo -e "  ${GREEN}✓${RESET} $1"; }
info() { echo -e "  ${DIM}·${RESET} $1"; }
warn() { echo -e "  ${YELLOW}!${RESET} $1"; }
fail() { echo -e "  ${RED}✗${RESET} $1"; }
step() { echo -e "\n${BOLD}$1${RESET}"; }

is_pid_alive() {
  local pid_file="$1"
  [[ -f "$pid_file" ]] || return 1
  local pid
  pid="$(cat "$pid_file" 2>/dev/null)"
  [[ -z "$pid" ]] && return 1

  if command -v tasklist.exe >/dev/null 2>&1; then
    tasklist.exe /FI "PID eq $pid" 2>/dev/null | grep -q "$pid" && return 0
  fi
  kill -0 "$pid" 2>/dev/null
}

wait_for_http() {
  local url="$1" timeout="$2" name="$3"
  local waited=0
  while ! curl -fsS -o /dev/null "$url" 2>/dev/null; do
    sleep 1
    waited=$((waited + 1))
    if [[ $waited -ge $timeout ]]; then
      fail "$name did not respond at $url within ${timeout}s"
      return 1
    fi
  done
  return 0
}

# ---------------------------------------------------------------------------
# 0. Prerequisite check — venv and frontend deps must already exist.
# ---------------------------------------------------------------------------

step "Checking prerequisites"

# Locate Python in venv (Windows vs POSIX)
PYTHON_BIN=""
if [[ -x "$REPO_ROOT/venv/Scripts/python.exe" ]]; then
  PYTHON_BIN="$REPO_ROOT/venv/Scripts/python.exe"
elif [[ -x "$REPO_ROOT/venv/Scripts/python" ]]; then
  PYTHON_BIN="$REPO_ROOT/venv/Scripts/python"
elif [[ -x "$REPO_ROOT/venv/bin/python" ]]; then
  PYTHON_BIN="$REPO_ROOT/venv/bin/python"
elif [[ -x "$REPO_ROOT/venv/bin/python.exe" ]]; then
  PYTHON_BIN="$REPO_ROOT/venv/bin/python.exe"
fi

# Locate uvicorn
UVICORN_BIN=""
if [[ -x "$REPO_ROOT/venv/Scripts/uvicorn.exe" ]]; then
  UVICORN_BIN="$REPO_ROOT/venv/Scripts/uvicorn.exe"
elif [[ -x "$REPO_ROOT/venv/Scripts/uvicorn" ]]; then
  UVICORN_BIN="$REPO_ROOT/venv/Scripts/uvicorn"
elif [[ -x "$REPO_ROOT/venv/bin/uvicorn" ]]; then
  UVICORN_BIN="$REPO_ROOT/venv/bin/uvicorn"
elif [[ -n "$PYTHON_BIN" ]]; then
  UVICORN_BIN="$PYTHON_BIN -m uvicorn"
fi

if [[ -z "$PYTHON_BIN" ]]; then
  info "No venv/ found. Creating virtual environment in venv/ ..."
  SYS_PYTHON=""
  if command -v python3 >/dev/null 2>&1; then SYS_PYTHON="python3"
  elif command -v python >/dev/null 2>&1; then SYS_PYTHON="python"
  fi

  if [[ -z "$SYS_PYTHON" ]]; then
    fail "Python not found on PATH. Please install Python 3.11+ first."
    exit 1
  fi

  "$SYS_PYTHON" -m venv "$REPO_ROOT/venv" || { fail "Failed to create venv"; exit 1; }

  if [[ -x "$REPO_ROOT/venv/Scripts/python.exe" ]]; then
    PYTHON_BIN="$REPO_ROOT/venv/Scripts/python.exe"
    PIP_BIN="$REPO_ROOT/venv/Scripts/pip.exe"
  else
    PYTHON_BIN="$REPO_ROOT/venv/bin/python"
    PIP_BIN="$REPO_ROOT/venv/bin/pip"
  fi

  info "Installing Python dependencies (requirements.txt & requirements-backend.txt) ..."
  "$PIP_BIN" install -r "$REPO_ROOT/requirements.txt" -r "$REPO_ROOT/requirements-backend.txt" || { fail "pip install failed"; exit 1; }
  ok "Python dependencies installed successfully"
else
  ok "Python venv present ($PYTHON_BIN)"
fi

# Locate uvicorn after potential venv creation
UVICORN_BIN=""
if [[ -x "$REPO_ROOT/venv/Scripts/uvicorn.exe" ]]; then
  UVICORN_BIN="$REPO_ROOT/venv/Scripts/uvicorn.exe"
elif [[ -x "$REPO_ROOT/venv/Scripts/uvicorn" ]]; then
  UVICORN_BIN="$REPO_ROOT/venv/Scripts/uvicorn"
elif [[ -x "$REPO_ROOT/venv/bin/uvicorn" ]]; then
  UVICORN_BIN="$REPO_ROOT/venv/bin/uvicorn"
elif [[ -n "$PYTHON_BIN" ]]; then
  UVICORN_BIN="$PYTHON_BIN -m uvicorn"
fi

if [[ ! -d "$REPO_ROOT/frontend/node_modules" ]]; then
  info "frontend/node_modules missing. Installing frontend dependencies (npm install) ..."
  npm --prefix "$REPO_ROOT/frontend" install || { fail "npm install failed"; exit 1; }
  ok "Frontend dependencies installed successfully"
else
  ok "Frontend dependencies present"
fi

if [[ ! -d "$REPO_ROOT/datasets" ]]; then
  warn "datasets/ not found — replay and warmup will fail until it's placed at the repo root (see docs/DATASETS.md)"
else
  ok "datasets/ present"
fi

# ---------------------------------------------------------------------------
# 1. .env
# ---------------------------------------------------------------------------

step "Checking environment file"
if [[ ! -f "$REPO_ROOT/.env" ]]; then
  cp "$REPO_ROOT/.env.example" "$REPO_ROOT/.env"
  ok "Created .env from .env.example (defaults: aegis/aegis/aegis on 127.0.0.1:5432)"
else
  ok ".env present"
fi

# ---------------------------------------------------------------------------
# 2. Postgres — start it if it's not accepting connections, then ensure the
#    role/database/schema/seed exist. Every step here is idempotent.
# ---------------------------------------------------------------------------

step "Checking PostgreSQL"

PG_BIN=""
# Check Homebrew paths and standard Windows install paths
for candidate in \
  /opt/homebrew/opt/postgresql@16/bin \
  /opt/homebrew/opt/postgresql/bin \
  /usr/local/opt/postgresql@16/bin \
  /d/Program\ Files/PostgreSQL/*/bin \
  /c/Program\ Files/PostgreSQL/*/bin; do
  if compgen -G "$candidate/pg_isready*" > /dev/null; then
    # Expand glob if matches multiple
    for matched in $candidate; do
      if [[ -x "$matched/pg_isready" || -x "$matched/pg_isready.exe" ]]; then
        PG_BIN="$matched"
        break 2
      fi
    done
  fi
done

PG_ISREADY="${PG_BIN:+$PG_BIN/}pg_isready"
PSQL="${PG_BIN:+$PG_BIN/}psql"
CREATEUSER="${PG_BIN:+$PG_BIN/}createuser"
CREATEDB="${PG_BIN:+$PG_BIN/}createdb"
command -v "$PG_ISREADY" >/dev/null 2>&1 || PG_ISREADY="pg_isready"
command -v "$PSQL" >/dev/null 2>&1 || PSQL="psql"
command -v "$CREATEUSER" >/dev/null 2>&1 || CREATEUSER="createuser"
command -v "$CREATEDB" >/dev/null 2>&1 || CREATEDB="createdb"

if ! "$PG_ISREADY" -h 127.0.0.1 -p 5432 >/dev/null 2>&1; then
  info "Postgres not accepting connections yet — trying to start it"
  if command -v brew >/dev/null 2>&1; then
    brew services start postgresql@16 >/dev/null 2>&1 || brew services start postgresql >/dev/null 2>&1 || true
  elif command -v net.exe >/dev/null 2>&1; then
    net.exe start postgresql-x64-18 >/dev/null 2>&1 || net.exe start postgresql-x64-16 >/dev/null 2>&1 || true
  elif command -v powershell.exe >/dev/null 2>&1; then
    powershell.exe -NoProfile -Command "Get-Service *postgres* | Start-Service" >/dev/null 2>&1 || true
  fi
  waited=0
  until "$PG_ISREADY" -h 127.0.0.1 -p 5432 >/dev/null 2>&1; do
    sleep 1
    waited=$((waited + 1))
    if [[ $waited -ge 20 ]]; then
      fail "Postgres still not reachable after 20s. Please start PostgreSQL service and re-run."
      exit 1
    fi
  done
fi
ok "Postgres is accepting connections"

# Role + database: ignore "already exists" errors
"$CREATEUSER" -h 127.0.0.1 aegis --pwprompt 2>/dev/null || true
PGPASSWORD=aegis "$CREATEDB" -h 127.0.0.1 -U aegis aegis 2>/dev/null || true
if PGPASSWORD=aegis "$PSQL" -h 127.0.0.1 -U aegis -d aegis -c '\q' >/dev/null 2>&1; then
  ok "Database 'aegis' reachable as user 'aegis'"
else
  warn "Could not connect as aegis/aegis@aegis — if this is a fresh machine, see docs/SETUP.md for manual role/password setup"
fi

# Schema + seed — init_db is idempotent (create_all + upsert-by-name seed).
if PGPASSWORD=aegis "$PSQL" -h 127.0.0.1 -U aegis -d aegis -c '\q' >/dev/null 2>&1; then
  TABLE_COUNT="$(PGPASSWORD=aegis "$PSQL" -h 127.0.0.1 -U aegis -d aegis -tAc \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'" 2>/dev/null || echo 0)"
  if [[ "${TABLE_COUNT:-0}" -lt 5 ]]; then
    info "Schema missing or incomplete — running backend.init_db"
    if PYTHONPATH=src "$PYTHON_BIN" -m backend.init_db >>"$BACKEND_LOG" 2>&1; then
      ok "Schema created and assets seeded"
    else
      fail "backend.init_db failed — see $BACKEND_LOG"
      exit 1
    fi
  else
    ok "Schema already present ($TABLE_COUNT tables)"
  fi
fi

# ---------------------------------------------------------------------------
# 3. Model artifacts — build only what's missing.
# ---------------------------------------------------------------------------

step "Checking model artifacts"

if [[ -f "$REPO_ROOT/artifacts/streaming_scorer.joblib" ]]; then
  ok "streaming_scorer.joblib present"
else
  info "Building streaming_scorer.joblib (backend.warmup) — this fits on real Monday traffic, ~5s"
  if PYTHONPATH=src "$PYTHON_BIN" -m backend.warmup >>"$BACKEND_LOG" 2>&1; then
    ok "Built streaming_scorer.joblib"
  else
    fail "backend.warmup failed — see $BACKEND_LOG (needs datasets/ present)"
    exit 1
  fi
fi

if [[ -f "$REPO_ROOT/artifacts/supervised_flow_scorer.joblib" ]]; then
  ok "supervised_flow_scorer.joblib present"
else
  info "Building supervised_flow_scorer.joblib (backend.warmup_supervised) — ~4s"
  if PYTHONPATH=src "$PYTHON_BIN" -m backend.warmup_supervised >>"$BACKEND_LOG" 2>&1; then
    ok "Built supervised_flow_scorer.joblib"
  else
    warn "backend.warmup_supervised failed — the console will still start with two live channels instead of three. See $BACKEND_LOG"
  fi
fi

# ---------------------------------------------------------------------------
# 4. Backend
# ---------------------------------------------------------------------------

step "Starting backend (uvicorn)"

if [[ $RESTART -eq 1 ]] && is_pid_alive "$BACKEND_PID_FILE"; then
  info "Stopping existing backend (--restart)"
  OLD_PID="$(cat "$BACKEND_PID_FILE")"
  kill "$OLD_PID" 2>/dev/null || true
  command -v taskkill.exe >/dev/null 2>&1 && taskkill.exe /F /PID "$OLD_PID" 2>/dev/null || true
  sleep 1
fi

if is_pid_alive "$BACKEND_PID_FILE"; then
  ok "Backend already running (pid $(cat "$BACKEND_PID_FILE"))"
elif curl -fsS -o /dev/null "$BACKEND_URL/api/health" 2>/dev/null; then
  warn "Something is already answering on port $BACKEND_PORT (not started by this script) — leaving it alone"
else
  : > "$BACKEND_LOG"
  (
    cd "$REPO_ROOT"
    exec env PYTHONPATH=src $UVICORN_BIN backend.main:app --host "$BACKEND_HOST" --port "$BACKEND_PORT"
  ) >>"$BACKEND_LOG" 2>&1 &
  echo $! > "$BACKEND_PID_FILE"
  disown 2>/dev/null || true
  info "Launched (pid $!), waiting for $BACKEND_URL/api/health ..."
  if wait_for_http "$BACKEND_URL/api/health" 30 "Backend"; then
    ok "Backend is up at $BACKEND_URL"
  else
    fail "Backend failed to become healthy — tail $BACKEND_LOG"
    exit 1
  fi
fi

HEALTH_JSON="$(curl -fsS "$BACKEND_URL/api/health" 2>/dev/null || echo '{}')"
echo "$HEALTH_JSON" | grep -q '"scorer_loaded":true' && ok "Detection model loaded" || warn "Detection model not loaded — check $BACKEND_LOG"

# ---------------------------------------------------------------------------
# 5. Frontend
# ---------------------------------------------------------------------------

step "Starting frontend (Next.js)"

if [[ $RESTART -eq 1 ]] && is_pid_alive "$FRONTEND_PID_FILE"; then
  info "Stopping existing frontend (--restart)"
  OLD_PID="$(cat "$FRONTEND_PID_FILE")"
  kill "$OLD_PID" 2>/dev/null || true
  command -v taskkill.exe >/dev/null 2>&1 && taskkill.exe /F /PID "$OLD_PID" 2>/dev/null || true
  sleep 1
fi

if is_pid_alive "$FRONTEND_PID_FILE"; then
  ok "Frontend already running (pid $(cat "$FRONTEND_PID_FILE"))"
elif curl -fsS -o /dev/null "$FRONTEND_URL" 2>/dev/null; then
  warn "Something is already answering on port $FRONTEND_PORT (not started by this script) — leaving it alone"
else
  : > "$FRONTEND_LOG"
  (
    cd "$REPO_ROOT"
    exec npm --prefix frontend run dev -- --port "$FRONTEND_PORT"
  ) >>"$FRONTEND_LOG" 2>&1 &
  echo $! > "$FRONTEND_PID_FILE"
  disown 2>/dev/null || true
  info "Launched (pid $!), waiting for $FRONTEND_URL ..."
  if wait_for_http "$FRONTEND_URL" 60 "Frontend"; then
    ok "Frontend is up at $FRONTEND_URL"
  else
    fail "Frontend failed to become healthy — tail $FRONTEND_LOG"
    exit 1
  fi
fi

# ---------------------------------------------------------------------------
# 6. Replay
# ---------------------------------------------------------------------------

if [[ $START_REPLAY -eq 1 ]]; then
  step "Starting replay"
  STATS_JSON="$(curl -fsS "$BACKEND_URL/api/stats" 2>/dev/null || echo '{}')"
  if echo "$STATS_JSON" | grep -q '"running":true'; then
    ok "Replay already running"
  else
    RESP="$(curl -fsS -X POST "$BACKEND_URL/api/replay/start" \
      -H 'Content-Type: application/json' \
      -d "{\"dataset\":\"${REPLAY_DATASET}\",\"speed\":${REPLAY_SPEED}}" 2>&1)"
    if echo "$RESP" | grep -q '"running":true'; then
      ok "Replay started: $REPLAY_DATASET at ${REPLAY_SPEED}x"
    else
      warn "Could not start replay automatically: $RESP"
      warn "You can start it from the console header, or: curl -X POST $BACKEND_URL/api/replay/start -H 'Content-Type: application/json' -d '{\"dataset\":\"$REPLAY_DATASET\",\"speed\":$REPLAY_SPEED}'"
    fi
  fi
else
  step "Replay"
  info "Skipped (--no-replay) — start it from the console header, or POST $BACKEND_URL/api/replay/start"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

step "AEGIS is up"
echo -e "  Console:  ${BOLD}${FRONTEND_URL}${RESET}"
echo -e "  API:      ${BOLD}${BACKEND_URL}${RESET}  (docs at ${BACKEND_URL}/docs)"
echo -e "  Logs:     ${DIM}${BACKEND_LOG}${RESET}"
echo -e "            ${DIM}${FRONTEND_LOG}${RESET}"
echo -e "  Stop:     ${DIM}scripts/dev-down.sh${RESET}"
echo ""
