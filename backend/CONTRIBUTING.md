# Contributing

## Before You Start

This repository currently treats the local workspace as canonical during active
development. Check the current release/status docs before assuming older git
history or archived notes are still accurate.

Relevant root docs:
- `../MMY_Public_Release_Status_2026-03-18.md`
- `../MMY_Canonical_Tracker.md`
- `../MMY_RC_Gate_Runbook.md`

## Setup

```bash
cd /mnt/i/AI\ Project/multi-mind-orchestrator
python3 -m pip install -e .[dev]
make smoke
make test
```

## Contribution Rules

- Keep changes scoped and avoid mixing unrelated cleanup with feature work.
- Do not revert local changes you did not author unless explicitly directed.
- Prefer small patches with verification notes.
- Add or update tests for behavior changes when practical.
- Keep security-sensitive changes explicit and documented.

## Validation

Minimum validation for most changes:

```bash
make test
bash /mnt/i/AI\ Project/run_rc_sweep.sh
```

For release-sensitive work, also refresh the relevant acceptance or pentest
artifacts described in the root MMY status docs.

## Style

- Backend: Python 3.11+
- Dashboard: Next.js + TypeScript
- Prefer clear, direct naming over clever abstractions
- Preserve existing security and audit behavior unless intentionally changing it
