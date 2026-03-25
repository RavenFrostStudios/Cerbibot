# Contributing

## Scope

This repository is the dashboard/UI for CerbiBot.

Keep UI work aligned with the backend API and current public product documentation.

## Setup

```bash
cd dashboard
npm install
npx tsc --noEmit
npm run build -- --webpack
```

## Rules

- Do not commit `node_modules/` or `.next/`
- Keep changes scoped
- Prefer type-safe API boundaries
- Do not reintroduce `typescript.ignoreBuildErrors`
- Preserve the current product story: local/self-hosted operator dashboard

## Validation

Minimum validation for dashboard changes:

```bash
npx tsc --noEmit
npm run build -- --webpack
```
