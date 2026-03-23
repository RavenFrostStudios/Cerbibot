# Docs Section Copy

## Section Title

Docs

## Section Intro

CerbiBot is available as a self-hosted technical preview. The backend, dashboard, and setup material are public so you can inspect the architecture and run the stack locally.

## Suggested Cards

### Backend Repo

Multi-Mind Orchestrator powers CerbiBot's local daemon, orchestration modes, governed skills, provider controls, artifacts, and audit surfaces.

Link label:
`View Backend Repo`

Suggested target:
`https://github.com/RavenFrostStudios/multi-mind-orchestrator`

### Dashboard Repo

The CerbiBot dashboard provides operator visibility for runs, settings, heartbeats, provider configuration, and project-scoped workflow management.

Link label:
`View Dashboard Repo`

Suggested target:
`https://github.com/RavenFrostStudios/ai-orchestrator-dashboard`

### Local Setup

Run CerbiBot locally with the daemon and dashboard stack. Setup is intended for technical users who want direct control over models, routing, and execution.

Link label:
`Run Locally`

Suggested content block:

```bash
cd "/path/to/multi-mind-orchestrator"
python3 -m mmctl serve --host 127.0.0.1 --port 8100

cd "/path/to/ai-orchestrator-dashboard"
npm run dev
```

Optional compact note:

CerbiBot currently targets users comfortable running a local daemon and dashboard during the technical preview phase.
