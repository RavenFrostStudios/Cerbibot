#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

BASELINE_POLICY="${1:-policies/base.yaml}"
CURRENT_POLICY="${2:-policies/development.yaml}"

python3 -m mmctl policy check --path policies
DIFF_OUT="$(python3 -m mmctl policy diff "$BASELINE_POLICY" "$CURRENT_POLICY")"
echo "$DIFF_OUT"

if echo "$DIFF_OUT" | rg -q "widening.*[^-]"; then
  echo "Policy widening detected. Review required."
  exit 1
fi

echo "Policy check passed."
