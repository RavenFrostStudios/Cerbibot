# CerbiBot Dashboard

Next.js dashboard for the CerbiBot backend.

This is the operator UI layer for CerbiBot.

## What It Includes

- chat interface
- runs view with heartbeat/stalled-run visibility
- settings and provider controls
- MCP configuration surfaces
- skills management
- memory view
- artifacts view

## Local Development

```bash
cd dashboard
npm install
npm run dev
```

Default local URL:

```text
http://localhost:3000
```

## Production Build

```bash
cd dashboard
npm run build -- --webpack
```

## Backend Dependency

The dashboard expects the MMY daemon API to be running separately.

Typical backend startup:

```bash
cd backend
python3 -m mmctl serve --host 127.0.0.1 --port 8100
```

Or use the monorepo helper:

```bash
bash scripts/run_local_stack.sh
```

## Current State

This dashboard is part of the current CerbiBot public technical preview stack.

It now passes:
- TypeScript type-checking
- production build without `ignoreBuildErrors`
