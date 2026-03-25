# Contributing

## Before You Start

Keep changes scoped and assume the monorepo is the public source of truth.

## Setup

```bash
cd backend
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
```

## Style

- Backend: Python 3.11+
- Dashboard: Next.js + TypeScript
- Prefer clear, direct naming over clever abstractions
- Preserve existing security and audit behavior unless intentionally changing it
