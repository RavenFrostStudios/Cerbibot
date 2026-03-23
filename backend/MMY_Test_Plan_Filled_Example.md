# MMY Test Plan (Filled Example)

This is an example of how to fill `MMY_Test_Plan_and_Results_Template.md`.

## 1) Test Process (Example Run)

1. Restarted server + dashboard.
2. Ran doctor and saved JSON report.
3. Ran live acceptance with local provider.
4. Executed manual tests for providers, modes, web, memory, skills, UI.
5. Logged issues in one table.
6. Wrote release summary and go/no-go.

## 2) Environment Snapshot

- Date/Time: 2026-02-16 09:30 local
- Tester: Raven
- OS: WSL2 Ubuntu on Windows 11
- Branch/commit: `main` / `abc1234` (example)
- Server command used: `python3 -m mmctl serve --config config/config.example.yaml`
- Dashboard version/runtime: Next.js dev server
- Provider(s) enabled: `local (gemma3:4b)`, `google (gemini-2.5-flash)` (example)

## 3) Doctor + Acceptance

### 3.1 Doctor
- Command: `python3 -m mmctl doctor --smoke-providers --json-out /tmp/mmy-doctor.json`
- Result: `PASS`
- Passed: 11
- Failed: 0
- Skipped: 4
- Notes: Disabled providers skipped as expected.

### 3.2 Live Acceptance
- Command:
  `python3 scripts/live_acceptance_check.py --base-url http://127.0.0.1:8100 --token "<TOKEN>" --provider local --provider-model gemma3:4b --request-timeout-seconds 90 --out /tmp/mmy-live-modes.json`
- Result: `PASS`
- Passed: 12
- Failed: 0
- Skipped: 2
- Notes: Skips were expected (`admin-password` not provided; mutating provider test not requested).

## 4) Provider + Routing Tests

- [x] Provider key save/load works.
- [x] Apply to daemon works on first try.
- [x] Auto-fix routes works when provider mismatch exists.
- [x] Provider connection test works for enabled providers.
- [x] Smoke test reports correctly.
- [x] Role routing persists after restart.

Notes:
- One transient route mismatch recovered via auto-fix.

## 5) Chat Mode Tests

- [x] Single
- [x] Critique
- [x] Debate
- [x] Consensus
- [x] Council
- [x] Web

For each mode:
- [x] Reply generated
- [x] No malformed formatting leakage
- [x] Artifacts created
- [x] Run status updates correctly

Notes:
- Local model slower on multi-stage modes, but outputs completed.

## 6) Web Reliability + Citations

- [x] Search query returned grounded answer with citations.
- [x] Direct URL query worked.
- [x] Weather fallback worked when search was challenged.
- [x] Warning behavior acceptable for non-debug mode.
- [x] Source-limit setting respected.
- [x] User-facing mode label shows `web`.

Sample prompts:
- “What is the current weather in Montreal, Quebec, Canada? Include source URLs and retrieval timestamps only.”
- “Can you find the web address for the Forgotten Realms lore website?”
- “What is the current price of bitcoin right now?”

Notes:
- Some sites returned 403; model still grounded from alternate sources.

## 7) Memory Behavior

- [x] Manual memory add/delete works.
- [x] Duplicate prevention works.
- [x] Memory suggestion flow works.
- [x] Already-stored indicators appear.
- [x] Relevant memory retrieval works.
- [x] Irrelevant memory suppressed for web-style query.
- [x] Profile/meta leakage removed from final response.

Prompts:
- “Remember this exact phrase: RAVEN-APPLE-42”
- “Can you look up a good website for free llm models?”

Notes:
- Previous irrelevant memory leak fixed by relevance gating.

## 8) Skills System

### 8.1 Draft + Validation
- [x] Validate draft works.
- [x] Test draft works.
- [x] Save skill works.

### 8.2 Installed Skills
- [x] Enable/disable works.
- [x] Run from UI works.
- [x] Run via API works.
- [x] Export works.
- [x] Import works (disabled by default).
- [x] Delete works with themed confirmation modal.

### 8.3 Curated Catalog
- [x] Load
- [x] Validate
- [x] Test
- [x] Install
- [x] Install + Test
- [x] Signature/trust badges render correctly

Notes:
- Governance modal table + rationale expansion works.

## 9) UI/UX and Stability

- [x] Provider card overlap fixed.
- [x] Query box focus behavior acceptable.
- [x] Themed modals active for core confirmations.
- [x] Artifact title/url wrapping clean.
- [x] Artifact single/bulk delete works.
- [x] No blocking hydration/chunk errors in normal flow.
- [x] Theme switching works across previews.

Notes:
- Minor visual spacing tweaks still possible.

## 10) Release Readiness Summary

- Critical issues open: 0
- High issues open: 0
- Medium issues open: 2
- Low issues open: 3
- Overall status: `GO` (internal) / `BETA-GO` (external with monitoring)
- Reason: Core functionality stable, no blocking regressions.

## 11) Issue Log (Example)

| ID | Severity | Area | Repro Steps | Expected | Actual | Evidence (run/artifact/log) | Status |
|---|---|---|---|---|---|---|---|
| 1 | Medium | Web | Query weather repeatedly in web mode | Always grounded with search results | Fallback used when search challenged | `run-...`, daemon logs, artifact citations | Open |
| 2 | Medium | UX | Run long multi-mode with local model | Fast completion | Slow but completes | elapsed time in run details | Open |
| 3 | Low | UI | Open skills governance modal on small display | No clipping | Minor table overflow in narrow viewport | screenshot | Open |
| 4 | Low | UX copy | Warning text in edge fallback | Clear, user-friendly | Slightly technical wording | artifact warning block | Open |
| 5 | Low | Settings | Rapid provider toggles + apply | Stable status labels | Rare stale status message | manual repro notes | Open |

