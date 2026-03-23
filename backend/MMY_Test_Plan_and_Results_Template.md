# MMY Test Plan and Results Template

Use this file as your single-run QA checklist and report.

## 1) Test Process (Use This Every Run)

1. Start clean:
   - Restart server/daemon before testing.
   - Clear stale sessions/runs if needed.
2. Baseline health:
   - Run `python3 -m mmctl doctor --smoke-providers --json-out /tmp/mmy-doctor.json`
   - Save summary in the Results section.
3. Core acceptance:
   - Run `python3 scripts/live_acceptance_check.py --base-url http://127.0.0.1:8100 --token "<TOKEN>" --provider <provider> --provider-model <model> --request-timeout-seconds 90 --out /tmp/mmy-live-modes.json`
   - Record failed/skipped checks.
4. Manual UX + workflow tests:
   - Execute sections 2–9 below.
5. Log bugs in one pass:
   - Add each issue in section 11 with reproducible steps and evidence.
6. Final summary:
   - Fill section 10 and decide `GO / NO-GO`.

## 2) Environment Snapshot

- Date/Time:
- Tester:
- OS:
- Branch/commit:
- Server command used:
- Dashboard version/runtime:
- Provider(s) enabled:

## 3) Doctor + Acceptance

### 3.1 Doctor
- Command:
- Result: `PASS / FAIL`
- Passed:
- Failed:
- Skipped:
- Notes:

### 3.2 Live Acceptance
- Command:
- Result: `PASS / FAIL`
- Passed:
- Failed:
- Skipped:
- Notes:

## 4) Provider + Routing Tests

- [ ] Provider key save/load works.
- [ ] Apply to daemon works on first try.
- [ ] Auto-fix routes works when provider disabled mismatch exists.
- [ ] Provider connection test works for each enabled provider.
- [ ] Smoke test reports correctly.
- [ ] Role routing persists after restart.

Notes:

## 5) Chat Mode Tests

Test each mode with one prompt and one follow-up:
- [ ] Single
- [ ] Critique
- [ ] Debate
- [ ] Consensus
- [ ] Council
- [ ] Web

For each mode verify:
- [ ] Reply generated
- [ ] No malformed formatting leakage
- [ ] Artifacts created
- [ ] Run status updates correctly

Notes:

## 6) Web Reliability + Citations

- [ ] Query needing search returns grounded answer with citations.
- [ ] Direct URL query works.
- [ ] Weather query fallback works when search blocked.
- [ ] Warning behavior is user-appropriate (no noisy internal warnings unless debug enabled).
- [ ] Source-limit setting respected.
- [ ] `web` label shown consistently (not old `retrieval` wording where user-facing).

Sample prompts used:
- 
- 
- 

Notes:

## 7) Memory Behavior

- [ ] Manual memory add/delete works.
- [ ] Duplicate prevention works.
- [ ] Memory suggestion flow works.
- [ ] “Already stored” indicators appear correctly.
- [ ] Memory retrieval in chat works when relevant.
- [ ] Irrelevant memory is NOT injected into unrelated/web queries.
- [ ] No profile/meta leakage in response prefix.

Prompts used:
- 
- 

Notes:

## 8) Skills System

### 8.1 Draft + Validation
- [ ] Validate draft works.
- [ ] Test draft works.
- [ ] Save skill works.

### 8.2 Installed Skills
- [ ] Enable/disable works.
- [ ] Run from UI works.
- [ ] Run via API works.
- [ ] Export works.
- [ ] Import works (disabled by default after import).
- [ ] Delete works with themed confirmation modal.

### 8.3 Curated Catalog
- [ ] Load
- [ ] Validate
- [ ] Test
- [ ] Install
- [ ] Install + Test
- [ ] Signature/trust badges render correctly

Notes:

## 9) UI/UX and Stability

- [ ] No major layout overlap on provider cards.
- [ ] Focus starts in query box.
- [ ] Themed modals used (not browser default confirm for core flows).
- [ ] Artifact titles and long URLs wrap/trim cleanly.
- [ ] Artifact delete controls work (single + bulk).
- [ ] Hydration/chunk load errors not present during normal use.
- [ ] Theme switch behavior correct (light/dark/cyberpunk/stealth previews).

Notes:

## 10) Release Readiness Summary

- Critical issues open:
- High issues open:
- Medium issues open:
- Low issues open:
- Overall status: `GO / NO-GO`
- Reason:

## 11) Issue Log (Fill During Testing)

Use one row per issue.

| ID | Severity | Area | Repro Steps | Expected | Actual | Evidence (run/artifact/log) | Status |
|---|---|---|---|---|---|---|---|
| 1 |  |  |  |  |  |  |  |
| 2 |  |  |  |  |  |  |  |

## 12) Optional Commands Reference

```bash
# Doctor
python3 -m mmctl doctor --smoke-providers --json-out /tmp/mmy-doctor.json

# Acceptance (example)
python3 scripts/live_acceptance_check.py \
  --base-url http://127.0.0.1:8100 \
  --token "<TOKEN>" \
  --provider local \
  --provider-model gemma3:4b \
  --request-timeout-seconds 90 \
  --out /tmp/mmy-live-modes.json

# Quick failed-check extraction
python3 - <<'PY'
import json
p = "/tmp/mmy-live-modes.json"
with open(p, "r", encoding="utf-8") as f:
    r = json.load(f)
print(r.get("summary", ""))
for c in r.get("checks", []):
    if c.get("status") != "PASS":
        print(c.get("status", ""), c.get("name", ""), "=>", c.get("detail", ""))
PY
```

