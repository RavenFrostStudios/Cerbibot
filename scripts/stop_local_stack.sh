#!/usr/bin/env bash
set -euo pipefail

STATE_DIR="/tmp/cerbibot-local-stack"
PID_FILE="$STATE_DIR/pids.env"

if [[ ! -f "$PID_FILE" ]]; then
  echo "No running MMY local stack state found."
  exit 0
fi

# shellcheck disable=SC1090
source "$PID_FILE"

stop_pid() {
  local pid="${1:-}"
  if [[ -z "$pid" ]]; then
    return 0
  fi
  if kill -0 "$pid" >/dev/null 2>&1; then
    kill "$pid" >/dev/null 2>&1 || true
    sleep 1
    if kill -0 "$pid" >/dev/null 2>&1; then
      kill -9 "$pid" >/dev/null 2>&1 || true
    fi
  fi
}

stop_pid "${DASH_PID:-}"
stop_pid "${DELEGATE_PID:-}"
stop_pid "${ORCH_PID:-}"

rm -f "$PID_FILE"
echo "MMY local stack stopped."
