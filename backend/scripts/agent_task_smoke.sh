#!/usr/bin/env bash
set -euo pipefail

TASK="${1:-task_03_health_snapshot}"
PROVIDER="${2:-xai}"
MODEL="${3:-grok-4-1-fast-reasoning}"
CONFIG="${4:-config/config.example.yaml}"

BASE_DIR="evaluation/agent_tasks/${TASK}"
INPUT_JOB="${BASE_DIR}/job.jsonl"
OUT_FILE="${BASE_DIR}/results.jsonl"
TMP_JOB="${BASE_DIR}/.job.override.jsonl"

if [[ ! -f "${INPUT_JOB}" ]]; then
  echo "task job not found: ${INPUT_JOB}" >&2
  exit 1
fi

python3 - <<PY
import json
from pathlib import Path
inp = Path(${INPUT_JOB@Q})
out = Path(${TMP_JOB@Q})
row = json.loads(inp.read_text(encoding='utf-8').strip())
row['provider'] = ${PROVIDER@Q}
row['model'] = ${MODEL@Q}
if not row.get('query') and row.get('prompt'):
    row['query'] = row['prompt']
out.write_text(json.dumps(row, ensure_ascii=True) + "\n", encoding='utf-8')
PY

python3 -m mmctl batch run "${TMP_JOB}" \
  --output-file "${OUT_FILE}" \
  --parallel 1 \
  --config "${CONFIG}"

python3 scripts/agent_task_check.py \
  --task "${TASK}" \
  --results "${OUT_FILE}" \
  --require-no-degradation

echo "smoke task passed: ${TASK} provider=${PROVIDER} model=${MODEL}"
