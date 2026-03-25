#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ORCH_DIR="$ROOT/backend"
DASH_DIR="$ROOT/dashboard"
STATE_DIR="/tmp/cerbibot-local-stack"
PID_FILE="$STATE_DIR/pids.env"

HOST="${CERBIBOT_HOST:-127.0.0.1}"
PORT="${CERBIBOT_PORT:-8100}"
DASH_PORT="${CERBIBOT_DASH_PORT:-3000}"
START_DASHBOARD=1
START_DELEGATE=1

usage() {
  cat <<'EOF'
Usage:
  bash scripts/run_local_stack.sh [options]

Options:
  --no-dashboard   Start backend + delegate daemon only
  --no-delegate    Start backend + dashboard only
  --backend-only   Start backend only
  --help           Show this help

Environment overrides:
  CERBIBOT_HOST
  CERBIBOT_PORT
  CERBIBOT_DASH_PORT
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-dashboard)
      START_DASHBOARD=0
      shift
      ;;
    --no-delegate)
      START_DELEGATE=0
      shift
      ;;
    --backend-only)
      START_DASHBOARD=0
      START_DELEGATE=0
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

mkdir -p "$STATE_DIR"

if [[ -f "$PID_FILE" ]]; then
  echo "Existing stack state found at $PID_FILE" >&2
  echo "Run: bash scripts/stop_local_stack.sh" >&2
  exit 1
fi

ORCH_LOG="$STATE_DIR/orchestrator.log"
DELEGATE_LOG="$STATE_DIR/delegate.log"
DASH_LOG="$STATE_DIR/dashboard.log"

start_bg() {
  local name="$1"
  local log="$2"
  shift 2
  (
    "$@"
  ) >"$log" 2>&1 &
  local pid=$!
  echo "$pid"
}

echo "Starting CerbiBot local stack..."

ORCH_PID=$(start_bg orchestrator "$ORCH_LOG" bash -lc "cd \"$ORCH_DIR\" && python3 -m mmctl serve --host $HOST --port $PORT")
sleep 1

DELEGATE_PID=""
if [[ "$START_DELEGATE" -eq 1 ]]; then
  DELEGATE_PID=$(start_bg delegate "$DELEGATE_LOG" bash -lc "cd \"$ORCH_DIR\" && python3 -m mmctl delegate daemon")
fi

DASH_PID=""
if [[ "$START_DASHBOARD" -eq 1 ]]; then
  DASH_PID=$(start_bg dashboard "$DASH_LOG" bash -lc "cd \"$DASH_DIR\" && npm run dev -- --port $DASH_PORT")
fi

cat >"$PID_FILE" <<EOF
ORCH_PID=$ORCH_PID
DELEGATE_PID=$DELEGATE_PID
DASH_PID=$DASH_PID
HOST=$HOST
PORT=$PORT
DASH_PORT=$DASH_PORT
ORCH_LOG=$ORCH_LOG
DELEGATE_LOG=$DELEGATE_LOG
DASH_LOG=$DASH_LOG
EOF

cat <<EOF
CerbiBot local stack started.

Backend:
  URL: http://$HOST:$PORT
  PID: $ORCH_PID
  Log: $ORCH_LOG

Delegate daemon:
  Enabled: $([[ "$START_DELEGATE" -eq 1 ]] && echo yes || echo no)
  PID: ${DELEGATE_PID:-n/a}
  Log: $DELEGATE_LOG

Dashboard:
  Enabled: $([[ "$START_DASHBOARD" -eq 1 ]] && echo yes || echo no)
  URL: http://$HOST:$DASH_PORT
  PID: ${DASH_PID:-n/a}
  Log: $DASH_LOG

Stop everything with:
  bash scripts/stop_local_stack.sh
EOF
