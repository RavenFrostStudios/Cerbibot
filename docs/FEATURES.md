# Current Features

Updated: 2026-03-21
Scope: current local state of `multi-mind-orchestrator` + `ai-orchestrator-dashboard`

## Product Summary

CerbiBot is a local/self-hosted orchestration platform for multi-mode LLM work with:
- governed skills
- project-scoped state
- provider routing and diagnostics
- daemon-backed API and dashboard
- artifacts, audit, and delegation support

## Core User Modes

Available orchestration modes:
- `single`
- `critique`
- `retrieval`
- `debate`
- `consensus`
- `council`

Available through:
- CLI (`mmctl ask`, `mmctl chat`)
- HTTP API (`/v1/ask`, `/v1/chat`)
- dashboard chat UI

## Dashboard Surfaces

Current dashboard pages:
- chat: `/`
- dashboard: `/dashboard`
- runs: `/runs`
- settings: `/settings`
- memory: `/memory`
- skills: `/skills`
- artifacts: `/artifacts`

Current dashboard capabilities:
- chat and session management
- project selection
- live run monitoring
- heartbeat/stalled-run display
- provider configuration
- role-routing controls
- MCP settings and health management
- skill catalog / local skill management / draft testing
- artifact browsing and export
- memory review and management

## Backend / API Capabilities

Current major API groups:
- health
- provider config, keys, testing, catalog, models
- role routing
- ask/chat execution
- sessions and DAG view
- runs, run events, heartbeat, resume, dependencies
- run triggers and webhook execution
- tool approvals
- memory and memory suggestions
- projects
- artifacts and artifact export/delete
- skills, skill catalog, skill import/export/enable/disable/delete/test
- draft skill validation/save
- tool simulation
- server doctor
- admin password and token recovery/rotation
- audit logging and security events
- delegate health/jobs
- MCP server config/health
- remote-access configuration and health

## Security / Governance

Implemented security and governance features:
- bearer-token protected daemon API
- admin-password gated sensitive actions
- lockout flow for repeated admin auth failures
- audit event logging
- encrypted-at-rest support for local data surfaces
- artifact export authorization checks
- project-scoped session and memory isolation
- skill manifest requirements
- skill signing and verification support
- approval flow for risky tool calls
- no-fake-success work is partially implemented and still tracked as a polish area

## Skills

Current skill capabilities include:
- local skill registry
- curated skill catalog flow
- workflow skill install / enable / disable / delete
- export and import
- test and adversarial test paths
- draft validation and draft save APIs
- governance analyzer for overlap / merge / crossover candidates

Governance analyzer artifacts:
- `merge_candidates.json`
- `crossover_candidates.json`
- `skills_bloat_report.md`
- `deprecation_plan.md`

## Delegation

Current delegate support includes:
- delegate daemon
- health endpoint
- job submit/list/show/fetch/follow/apply
- patch-first artifact workflow
- non-git fallback behavior
- deny-glob enforcement
- intent-drift protections

Current state:
- backend delegate-focused tests are passing
- operator UX exists, but public-facing explanation is still lighter than it should be

## Operations

Current operational surfaces:
- `mmctl doctor`
- release-candidate sweep script
- live acceptance harness
- local pentest harness
- status refresh scripts
- local one-command stack startup:
  - `../run_local_stack.sh`
  - `../stop_local_stack.sh`

## Remote / External Integration

Current integrations and adjacent surfaces:
- MCP server config and health
- provider model discovery/test
- remote-access plan APIs
- webhook-triggered run execution
- xAI provider usage verified in recent live acceptance

Not yet full productized connector surfaces:
- Discord as a full public-ready connector platform
- Telegram
- email/calendar/cloud-doc/db connectors

## What Is Strong Right Now

- orchestration depth
- governance and approval posture
- project isolation
- artifact and audit handling
- release-gate thinking
- local operator workflow

## What Is Still More Limited

- public docs depth
- onboarding/storytelling polish
- connector breadth
- broader ecosystem packaging
- some operator UX polish for non-technical users
