# CerbiBot

CerbiBot is a security-first local AI orchestration platform from RavenFrost Studios.

This monorepo combines:
- `backend/`: the Multi-Mind Orchestrator daemon, CLI, orchestration logic, governed skills, artifacts, and control-plane APIs
- `dashboard/`: the CerbiBot operator dashboard
- `docs/`: public product and launch documentation
- `scripts/`: local stack startup and shutdown helpers

## Quick Start

```bash
git clone git@github.com:RavenFrostStudios/cerbibot.git
cd cerbibot
bash scripts/run_local_stack.sh
```

This starts:
- the backend on `http://127.0.0.1:8100`
- the delegate daemon
- the dashboard on `http://127.0.0.1:3000`

Stop the local stack with:

```bash
bash scripts/stop_local_stack.sh
```

## Repo Layout

```text
cerbibot/
  backend/
  dashboard/
  docs/
  scripts/
```

## Public Status

CerbiBot is currently positioned as a public technical preview.

Current release documentation lives under:
- `docs/LAUNCH_SUMMARY.md`
