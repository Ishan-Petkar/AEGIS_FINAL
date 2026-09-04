#!/usr/bin/env bash
# scripts/dev-open.sh — open the running AEGIS console (and optionally the
# API docs) in the default browser. Does not start anything itself — run
# scripts/dev-up.sh first if nothing responds.
#
# Cross-platform: works on Linux, macOS, and Windows (Git Bash / MSYS2 / WSL).
# On native Windows PowerShell, you can also use: scripts\dev-open.ps1
#
# Usage:
#   scripts/dev-open.sh          # open the console (http://127.0.0.1:3000)
#   scripts/dev-open.sh --api    # also open the API docs (.../docs)

set -uo pipefail

FRONTEND_URL="http://127.0.0.1:3000"
BACKEND_DOCS_URL="http://127.0.0.1:8000/docs"

OPEN_API=0
for arg in "$@"; do
  case "$arg" in
    --api) OPEN_API=1 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  esac
done

if [[ -t 1 ]]; then
  GREEN="\033[32m"; YELLOW="\033[33m"; DIM="\033[2m"; RESET="\033[0m"
else
  GREEN=""; YELLOW=""; DIM=""; RESET=""
fi

open_url() {
  local url="$1"
  if [[ "${OSTYPE:-}" == "msys" || "${OSTYPE:-}" == "cygwin" || "${OSTYPE:-}" == "win32" ]]; then
    cmd.exe /c start "" "$url" >/dev/null 2>&1
  elif command -v cmd.exe >/dev/null 2>&1; then
    cmd.exe /c start "" "$url" >/dev/null 2>&1
  elif command -v powershell.exe >/dev/null 2>&1; then
    powershell.exe -NoProfile -Command "Start-Process '$url'" >/dev/null 2>&1
  elif command -v open >/dev/null 2>&1; then          # macOS
    open "$url"
  elif command -v xdg-open >/dev/null 2>&1; then     # Linux
    xdg-open "$url" >/dev/null 2>&1 &
  else
    echo "Don't know how to open a browser on this system — open it yourself: $url"
    return 1
  fi
}

if curl -fsS -o /dev/null "$FRONTEND_URL" 2>/dev/null; then
  echo -e "  ${GREEN}✓${RESET} Console is up — opening $FRONTEND_URL"
  open_url "$FRONTEND_URL"
else
  echo -e "  ${YELLOW}!${RESET} Nothing is responding at $FRONTEND_URL yet."
  echo -e "  ${DIM}Run scripts/dev-up.sh first, then re-run this.${RESET}"
  exit 1
fi

if [[ $OPEN_API -eq 1 ]]; then
  if curl -fsS -o /dev/null "$BACKEND_DOCS_URL" 2>/dev/null; then
    echo -e "  ${GREEN}✓${RESET} Opening API docs — $BACKEND_DOCS_URL"
    open_url "$BACKEND_DOCS_URL"
  else
    echo -e "  ${YELLOW}!${RESET} Backend not responding at $BACKEND_DOCS_URL — skipping"
  fi
fi
