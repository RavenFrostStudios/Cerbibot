# MMY Orchestrator Dashboard

Next.js dashboard for the Multi-Mind Orchestrator backend.

This repo is the UI layer for CerbiBot. It is intended to run against the backend in:
- `multi-mind-orchestrator`

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
cd /mnt/i/AI\ Project/ai-orchestrator-dashboard
npm install
npm run dev
```

Default local URL:

```text
http://localhost:3000
```

## Production Build

```bash
cd /mnt/i/AI\ Project/ai-orchestrator-dashboard
npm run build -- --webpack
```

## Backend Dependency

The dashboard expects the MMY daemon API to be running separately.

Typical backend startup:

```bash
cd /mnt/i/AI\ Project/multi-mind-orchestrator
python3 -m mmctl serve --host 127.0.0.1 --port 8100
```

Or use the workspace helper:

```bash
bash /mnt/i/AI\ Project/run_local_stack.sh
```

## Current State

This dashboard is part of the current MMY public technical preview stack.

It now passes:
- TypeScript type-checking
- production build without `ignoreBuildErrors`
