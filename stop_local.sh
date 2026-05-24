#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="$ROOT_DIR/.local-dev"
API_PORT="${PIXELLE_API_PORT:-8500}"
WEB_PORT="${PIXELLE_WEB_PORT:-8501}"

stop_pid_file() {
  local pid_file="$1"
  if [ ! -f "$pid_file" ]; then
    return 0
  fi

  local pid
  pid="$(cat "$pid_file")"
  if [ -n "$pid" ] && kill -0 "$pid" >/dev/null 2>&1; then
    kill "$pid" >/dev/null 2>&1 || true
    echo "Stopped PID $pid"
  fi
  rm -f "$pid_file"
}

stop_project_listener() {
  local port="$1"
  local pids
  pids="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  for pid in $pids; do
    local command
    command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
    if [[ "$command" == *"$ROOT_DIR"* ]]; then
      kill "$pid" >/dev/null 2>&1 || true
      echo "Stopped Pixelle-Video listener on port $port, PID $pid"
    elif [ -n "$command" ]; then
      echo "Leaving non-project listener on port $port, PID $pid"
    fi
  done
}

stop_pid_file "$STATE_DIR/api.pid"
stop_pid_file "$STATE_DIR/web.pid"
stop_project_listener "$API_PORT"
stop_project_listener "$WEB_PORT"

echo "Pixelle-Video local dev stop command finished."
