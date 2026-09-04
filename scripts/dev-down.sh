#!/usr/bin/env bash
# scripts/dev-down.sh — stop everything scripts/dev-up.sh started: the
# Next.js console, the FastAPI backend, and (best-effort) the live replay.
# Postgres is left running (shared system service, not this project's to
# stop).
#
# Cross-platform: works on Linux, macOS, and Windows (Git Bash / MSYS2 / WSL).
# On native Windows PowerShell, you can also use: scripts\dev-down.ps1
#
# Robust by port, not just by PID: `npm run dev` can spawn child processes
# that outlive a plain `kill` on the recorded PID, so this also sweeps
# whatever is actually listening on 8000/3000 as a fallback.
#
# Usage: scripts/dev-down.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
DEV_DIR="$REPO_ROOT/.dev"

BACKEND_PID_FILE="$DEV_DIR/backend.pid"
FRONTEND_PID_FILE="$DEV_DIR/frontend.pid"
BACKEND_PORT="8000"
FRONTEND_PORT="3000"

if [[ -t 1 ]]; then
  BOLD="\033[1m"; DIM="\033[2m"; GREEN="\033[32m"; RESET="\033[0m"
else
  BOLD=""; DIM=""; GREEN=""; RESET=""
fi
ok()   { echo -e "  ${GREEN}✓${RESET} $1"; }
info() { echo -e "  ${DIM}·${RESET} $1"; }
step() { echo -e "\n${BOLD}$1${RESET}"; }

stop_by_pid_file() {
  local pid_file="$1" name="$2"
  if [[ -f "$pid_file" ]]; then
    local pid
    pid="$(cat "$pid_file" 2>/dev/null)"
    if [[ -n "${pid:-}" ]]; then
      # Try Windows taskkill if available
      if command -v taskkill.exe >/dev/null 2>&1; then
        taskkill.exe /F /T /PID "$pid" >/dev/null 2>&1 || true
      fi
      # Try POSIX pkill/kill
      pkill -P "$pid" 2>/dev/null || true
      kill "$pid" 2>/dev/null || true
      info "$name: stopped pid $pid"
    fi
    rm -f "$pid_file"
  fi
}

sweep_port() {
  local port="$1" name="$2"
  local pids=""

  if command -v lsof >/dev/null 2>&1; then
    pids="$(lsof -ti "tcp:${port}" -sTCP:LISTEN 2>/dev/null || true)"
    if [[ -n "$pids" ]]; then
      echo "$pids" | xargs kill 2>/dev/null || true
      sleep 1
      pids="$(lsof -ti "tcp:${port}" -sTCP:LISTEN 2>/dev/null || true)"
      [[ -n "$pids" ]] && echo "$pids" | xargs kill -9 2>/dev/null || true
      ok "$name: port $port freed"
    else
      ok "$name: nothing listening on port $port"
    fi
  elif command -v netstat.exe >/dev/null 2>&1 || command -v netstat >/dev/null 2>&1; then
    # Windows fallback using netstat
    local netstat_cmd="netstat"
    command -v netstat.exe >/dev/null 2>&1 && netstat_cmd="netstat.exe"
    pids="$($netstat_cmd -ano 2>/dev/null | awk -v p=":${port}" '$2 ~ p && $4 == "LISTENING" {print $5}' | tr -d '\r' | sort -u)"
    if [[ -n "$pids" ]]; then
      for p in $pids; do
        if [[ -n "$p" && "$p" -ne 0 ]]; then
          if command -v taskkill.exe >/dev/null 2>&1; then
            taskkill.exe /F /T /PID "$p" >/dev/null 2>&1 || true
          else
            kill -9 "$p" 2>/dev/null || true
          fi
        fi
      done
      ok "$name: port $port freed"
    else
      ok "$name: nothing listening on port $port"
    fi
  else
    ok "$name: checked port $port"
  fi
}

step "Stopping replay (best-effort)"
curl -fsS -X POST "http://127.0.0.1:${BACKEND_PORT}/api/replay/stop" >/dev/null 2>&1 \
  && info "Replay stop requested" \
  || info "Backend not reachable — skipping"

step "Stopping frontend"
stop_by_pid_file "$FRONTEND_PID_FILE" "Frontend"
sweep_port "$FRONTEND_PORT" "Frontend"

step "Stopping backend"
stop_by_pid_file "$BACKEND_PID_FILE" "Backend"
sweep_port "$BACKEND_PORT" "Backend"

echo -e "\n  ${DIM}PostgreSQL was left running — it's a shared system service.${RESET}\n"
