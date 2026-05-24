#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="$ROOT_DIR/.local-dev"
HOST="${PIXELLE_HOST:-127.0.0.1}"
API_PORT="${PIXELLE_API_PORT:-8500}"
WEB_PORT="${PIXELLE_WEB_PORT:-8501}"
PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
STREAMLIT_BIN="$ROOT_DIR/.venv/bin/streamlit"

mkdir -p "$STATE_DIR"

if [ ! -x "$PYTHON_BIN" ] || [ ! -x "$STREAMLIT_BIN" ]; then
  echo "Missing .venv dependencies. Run:"
  echo "  python3 -m venv .venv"
  echo "  .venv/bin/python -m pip install -e ."
  echo "  .venv/bin/python -m playwright install chromium"
  exit 1
fi

if [ ! -f "$ROOT_DIR/config.yaml" ]; then
  cp "$ROOT_DIR/config.example.yaml" "$ROOT_DIR/config.yaml"
  echo "Created config.yaml from config.example.yaml. Fill API keys before generation."
fi

check_port_free() {
  local port="$1"
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Port $port is already in use."
    lsof -nP -iTCP:"$port" -sTCP:LISTEN
    exit 1
  fi
}

wait_for_url() {
  local url="$1"
  local name="$2"
  for _ in $(seq 1 30); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      echo "$name is ready: $url"
      return 0
    fi
    sleep 1
  done
  echo "$name did not become ready: $url"
  return 1
}

check_port_free "$API_PORT"
check_port_free "$WEB_PORT"

nohup "$PYTHON_BIN" "$ROOT_DIR/api/app.py" \
  --host "$HOST" \
  --port "$API_PORT" \
  > "$STATE_DIR/api.log" 2>&1 &
echo "$!" > "$STATE_DIR/api.pid"

STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
STREAMLIT_SERVER_HEADLESS=true \
nohup "$STREAMLIT_BIN" run "$ROOT_DIR/web/app.py" \
  --server.port="$WEB_PORT" \
  --server.address="$HOST" \
  --browser.gatherUsageStats=false \
  --server.headless=true \
  > "$STATE_DIR/web.log" 2>&1 &
echo "$!" > "$STATE_DIR/web.pid"

wait_for_url "http://$HOST:$API_PORT/health" "API"
wait_for_url "http://$HOST:$WEB_PORT/_stcore/health" "Web"

echo "Pixelle-Video local dev is running."
echo "API: http://$HOST:$API_PORT"
echo "Web: http://$HOST:$WEB_PORT"
echo "Logs: $STATE_DIR"
