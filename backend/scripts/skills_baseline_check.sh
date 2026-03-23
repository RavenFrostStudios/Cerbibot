#!/usr/bin/env bash
set -uo pipefail

# Run a baseline smoke test across installed workflow skills.
# Usage:
#   scripts/skills_baseline_check.sh --token "<TOKEN>" [--base-url "http://127.0.0.1:8100"] [--out "/tmp/mmy-skills-baseline.json"]

BASE_URL="http://127.0.0.1:8100"
TOKEN=""
OUT_FILE=""
TIMEOUT_SECONDS=120

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-url)
      BASE_URL="${2:-}"
      shift 2
      ;;
    --token)
      TOKEN="${2:-}"
      shift 2
      ;;
    --out)
      OUT_FILE="${2:-}"
      shift 2
      ;;
    --timeout-seconds)
      TIMEOUT_SECONDS="${2:-120}"
      shift 2
      ;;
    -h|--help)
      cat <<'EOF'
Usage: scripts/skills_baseline_check.sh --token "<TOKEN>" [options]

Options:
  --base-url URL          API base URL (default: http://127.0.0.1:8100)
  --out PATH              Output JSON report path (default: /tmp/mmy-skills-baseline-<ts>.json)
  --timeout-seconds N     Per-request timeout seconds (default: 120)
  -h, --help              Show help
EOF
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$TOKEN" ]]; then
  echo "Error: --token is required" >&2
  exit 2
fi

if [[ -z "$OUT_FILE" ]]; then
  OUT_FILE="/tmp/mmy-skills-baseline-$(date +%s).json"
fi

TMP_NDJSON="/tmp/mmy-skills-baseline-lines-$$.ndjson"
: > "$TMP_NDJSON"

run_skill() {
  local skill="$1"
  local body="$2"
  local url="${BASE_URL%/}/v1/skills/${skill}/test"
  local resp
  local curl_rc=0
  resp="$(curl -sS -m "$TIMEOUT_SECONDS" -X POST "$url" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "$body")" || curl_rc=$?

  python3 - "$skill" "$curl_rc" "$resp" >> "$TMP_NDJSON" <<'PY'
import json
import sys

skill = sys.argv[1]
curl_rc = int(sys.argv[2])
raw = sys.argv[3]
row = {"skill": skill, "status": "FAIL", "reason": "", "steps_executed": None}

if curl_rc != 0:
    row["reason"] = f"curl_error={curl_rc}"
    print(json.dumps(row, ensure_ascii=True))
    raise SystemExit(0)

try:
    obj = json.loads(raw)
except Exception as exc:
    row["reason"] = f"non_json_response={type(exc).__name__}"
    print(json.dumps(row, ensure_ascii=True))
    raise SystemExit(0)

if isinstance(obj, dict) and "detail" in obj:
    row["reason"] = str(obj.get("detail"))
    print(json.dumps(row, ensure_ascii=True))
    raise SystemExit(0)

run = (obj or {}).get("run") or {}
steps = run.get("steps_executed")
if isinstance(steps, int):
    row["status"] = "PASS"
    row["steps_executed"] = steps
    row["reason"] = f"steps_executed={steps}"
else:
    row["reason"] = "missing run.steps_executed"

print(json.dumps(row, ensure_ascii=True))
PY
}

echo "Running skill baseline checks against ${BASE_URL%/} ..."

run_skill "repo_health_check" '{"run":true,"mode":"single"}'
run_skill "youtube_transcript_summary" '{"run":true,"mode":"single","input":{"url":"https://www.youtube.com/watch?v=dQw4w9WgXcQ"}}'
run_skill "youtube_transcript_fetch" '{"run":true,"mode":"single","input":{"transcript_url":"https://www.youtube.com/watch?v=dQw4w9WgXcQ"}}'
run_skill "web_research_brief" '{"run":true,"mode":"single","input":{"url_1":"https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Status","url_2":"https://developer.mozilla.org/en-US/docs/Web/API/Response/ok","url_3":"https://developer.mozilla.org/en-US/docs/Web/API/Fetch_API","topic":"http status and response.ok"}}'
run_skill "browser_task_runner" '{"run":true,"mode":"single","input":{"url":"https://en.wikipedia.org/wiki/Forgotten_Realms","objective":"Find an official lore website URL mentioned on the page","pattern":"official|website|fandom|wizards"}}'

python3 - "$TMP_NDJSON" "$OUT_FILE" <<'PY'
import json
import sys
from datetime import datetime, timezone

ndjson_path = sys.argv[1]
out_path = sys.argv[2]

rows = []
with open(ndjson_path, "r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))

passed = sum(1 for r in rows if r.get("status") == "PASS")
failed = sum(1 for r in rows if r.get("status") == "FAIL")
report = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "summary": {"passed": passed, "failed": failed, "total": len(rows)},
    "checks": rows,
}

with open(out_path, "w", encoding="utf-8") as f:
    json.dump(report, f, indent=2, ensure_ascii=True)

print("Summary:", report["summary"])
for r in rows:
    print(f"- {r['skill']}: {r['status']} ({r.get('reason','')})")
print(f"Wrote report: {out_path}")
PY

rm -f "$TMP_NDJSON"

