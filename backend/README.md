# CerbiBot Backend

Security-first orchestration backend for local and self-hosted CerbiBot workflows.

Multi-Mind Orchestrator focuses on:
- multi-mode orchestration: `single`, `critique`, `retrieval`, `debate`, `consensus`, `council`
- governed skill execution with approval and budget controls
- project-scoped sessions, memory, and artifacts
- provider management, diagnostics, and daemon-backed API control
- delegation workflows that produce patch-first artifacts

## Project Status

Current release posture:
- ready for public technical preview
- not yet positioned as a polished broad public release

## Repository Notes

- `LICENSE` defines the public license for this code.
- Security reporting guidance lives in `SECURITY.md`.
- Contribution guidance lives in `CONTRIBUTING.md`.

## Quickstart

```bash
cd backend
make setup
make smoke
make test
```

If `pytest` is not installed in your environment yet, run:

```bash
python3 -m pip install -e .[dev]
make test
```

Dashboard app:

```bash
cd dashboard
npm install
npm run build -- --webpack
```

## Core Commands

Common day-to-day commands:

```bash
# validate config
OPENAI_API_KEY=... ANTHROPIC_API_KEY=... python3 -m mmctl config check --config config/config.example.yaml

# run system diagnostics
python3 -m mmctl doctor
python3 -m mmctl doctor --smoke-providers --json-out /tmp/mmy-doctor.json

# ask in single mode
python3 -m mmctl ask "Explain CAP theorem"

# ask in critique mode
python3 -m mmctl ask --mode critique "Explain CAP theorem"

# ask in retrieval-grounded mode
python3 -m mmctl ask --mode retrieval "What is the latest version of Node.js?"

# ask in debate mode
python3 -m mmctl ask --mode debate "Should this project use microservices or a monolith?"

# ask in consensus mode
python3 -m mmctl ask --mode consensus "What is the capital of Australia?"

# ask in council mode
python3 -m mmctl ask --mode council "Design a secure authentication system"

# ask with tool-use planning (built-in and plugin tools)
python3 -m mmctl ask --tools "Use file_search then summarize matches" "Find TODOs in this repo"

# use local OpenAI-compatible endpoint
LOCAL_API_BASE=http://127.0.0.1:11434/v1 LOCAL_API_KEY=dummy python3 -m mmctl ask "Explain decorators"

# provider override examples
python3 -m mmctl ask --provider google "Explain transformers"
python3 -m mmctl ask --provider xai "Compare retrieval-augmented generation vs fine-tuning"

# force fact-checking in any mode
python3 -m mmctl ask --fact-check "Python 3.13 is the latest stable release."

# ask without streaming (good for piping)
python3 -m mmctl ask --no-stream "Explain CAP theorem"

# interactive chat mode
python3 -m mmctl chat --mode single

# show cost summary
python3 -m mmctl cost

# memory governance commands
python3 -m mmctl memory add "User prefers concise answers"
python3 -m mmctl memory list
python3 -m mmctl memory search "concise"
python3 -m mmctl memory delete 1

# run a declarative workflow skill
python3 -m mmctl skill run skills/summarize_repo.workflow.yaml --input '{"repo_path":"."}'
# acknowledge a shadow-run report for risky steps
python3 -m mmctl skill run skills/summarize_repo.workflow.yaml --input '{"repo_path":"."}' --shadow-confirm

# manage local skill registry
python3 -m mmctl skill install skills/summarize_repo.workflow.yaml
python3 -m mmctl skill list
python3 -m mmctl skill disable summarize_repo
python3 -m mmctl skill enable summarize_repo
python3 -m mmctl skill test summarize_repo
python3 -m mmctl skill test summarize_repo --run --shadow-confirm
python3 -m mmctl skill test summarize_repo --adversarial
python3 -m mmctl skill test summarize_repo --adversarial --fixtures evaluation/skills_adversarial/summarize_repo.yaml
python3 -m mmctl skill checksum summarize_repo
python3 -m mmctl skill keygen
python3 -m mmctl skill sign summarize_repo --private-key ~/.mmo/keys/skills_ed25519.pem
python3 -m mmctl skill verify summarize_repo --public-key ~/.mmo/keys/skills_ed25519.pub.pem

# secrets/keyring commands
python3 -m mmctl secret set openai_api_key "sk-..."
python3 -m mmctl secret list

# delegation gateway (patch-first artifacts from isolated git worktree)
python3 -m mmctl delegate health --socket ~/.mmo/delegate/run/delegate.sock
python3 -m mmctl delegate submit "Add tests for X" --repo . --file tests/test_x.py --check "pytest -q"
python3 -m mmctl delegate submit "Refactor module" --repo . --executor "python3 scripts/refactor.py --workspace {workspace}" --async-run
python3 -m mmctl delegate list
python3 -m mmctl delegate show job-abc123
python3 -m mmctl delegate fetch job-abc123
python3 -m mmctl delegate follow job-abc123
python3 -m mmctl delegate apply job-abc123 --check-only
python3 -m mmctl delegate daemon --socket ~/.mmo/delegate/run/delegate.sock
# daemon-backed usage
python3 -m mmctl delegate submit "Add tests for X" --repo . --socket ~/.mmo/delegate/run/delegate.sock --async-run
python3 -m mmctl delegate follow job-abc123 --socket ~/.mmo/delegate/run/delegate.sock

# run eval harness
python3 -m mmctl eval run

# run adversarial security eval suite
python3 -m mmctl eval adversarial

# benchmark critique roles (drafter/critic/refiner) and optionally apply best routes
python3 -m mmctl eval roles --strategy balanced
python3 -m mmctl eval roles --strategy quality --apply-best

# start HTTP daemon API
python3 -m mmctl serve --host 127.0.0.1 --port 8100
```

## Release Validation

Fast local release sweep:

```bash
bash scripts/run_local_stack.sh --backend-only
```

Workspace policy (`.delegate.yaml`) is supported at repo root for delegation jobs:

```yaml
context_roots:
  - .
allow_symlinks: true
symlink_policy: resolve_and_verify
deny_globs:
  - "**/.env"
  - "**/secrets/**"
  - "**/*key*"
```

Both requested files and changed files are checked against this policy.
If a repo has no `.git`, delegation automatically uses `temp_copy` mode and `delegate apply` uses `patch`.

## Skill Manifest Requirement

Workflow skills now require a `manifest` block. Install/test/run will fail if it is missing or invalid.

Required manifest fields:
- `purpose`
- `tools`
- `data_scope`
- `permissions`
- `approval_policy` (`draft_only`, `approve_actions`, `approve_high_risk`, `auto_execute_low_risk`)
- `rate_limits.actions_per_hour`
- `budgets.usd_cap`
- `kill_switch.enabled`
- `audit_sink`
- `failure_mode`

Risk handling behavior:
- CerbiBot computes a per-step risk level (`low`, `medium`, `high`) from tool type + argument markers.
- `approval_policy` controls escalation:
  - `draft_only`: blocks medium/high risk steps.
  - `approve_actions`: requires human approval for medium/high.
  - `approve_high_risk`: requires human approval for high only.
  - `auto_execute_low_risk`: blocks medium/high risk steps.

Minimal example:

```yaml
name: demo_skill
manifest:
  purpose: "Example skill"
  tools: [system_info]
  data_scope: ["local_runtime"]
  permissions: ["read", "model_call"]
  approval_policy: draft_only
  rate_limits:
    actions_per_hour: 10
  budgets:
    usd_cap: 1.0
  kill_switch:
    enabled: true
  audit_sink: "audit.jsonl"
  failure_mode: "safe_abort"
steps:
  - model_call: "hello"
    output: out
```

## Tool Plugins

Drop custom tools into repo-local `tools/<tool_name>/` with:
- `manifest.yaml`
- `handler.py` exposing `run(args: dict[str, str]) -> dict`

Example manifest:

```yaml
name: echo_text
description: Echo demo plugin
arg_schema:
  text: string
required_capabilities:
  - low_risk
sandbox_config:
  network_enabled: false
  cpu_limit: 1.0
  memory_limit_mb: 128
  timeout_seconds: 5
max_calls_per_request: 3
requires_human_approval: false
```

Example handler:

```python
def run(args: dict[str, str]) -> dict:
    text = args.get("text", "")
    return {"status": "ok", "tool": "echo_text", "stdout": text, "stderr": ""}
```

Discovered tools are auto-loaded by `orchestrator.tools.registry.load_tool_registry()`. Manifest-declared limits and capabilities are mapped into broker policy checks automatically.

## Make Targets

- `make setup` install package + dev dependencies
- `make smoke` run CLI/help, config check, and syntax compile checks
- `make test` run pytest
- `make lint` run ruff checks if installed
- `make typecheck` run mypy if installed
- `make format` run ruff formatter if installed
- `make clean` clear caches and build artifacts

Examples:

```bash
make smoke
make test
make clean
```

## Current Capabilities

- Retrieval-first mode with source fetching, sanitization, and citation output
- Fact-checking pipeline with verification notes (`--fact-check`, enabled by default in retrieval mode)
- Debate mode with two-round adversarial arguments and judge synthesis
- Consensus mode with parallel independent answers and evidence-based adjudication on disagreement
- Sandboxed `python_exec` tool path via capability broker (`--tools`)
- Read-only built-in tools: `file_read`, `file_search`, `git_status`, `web_retrieve`, `json_query`, `regex_test`, `system_info`
- Declarative workflow skills runner (`mmctl skill run <file|name> --input '{...}'`)
- Skill signing and verification (`mmctl skill sign|verify`) with optional install-time signature enforcement (`skills.require_signature`)
- Skill adversarial harness (`mmctl skill test --adversarial`) with YAML fixture cases and pass/fail summary
- Governed SQLite memory store and retrieval context (`mmctl memory ...`)
- Encryption at rest enabled by default (`security.data_protection`) for usage, audit, memory, and artifacts

When enabling encryption with `key_provider: passphrase`, set `MMO_MASTER_PASSPHRASE` (or configured `passphrase_env`); passphrase keys are derived with Argon2id.
- Adversarial fixture runner (`mmctl eval adversarial`) with pass/fail layer matrix
- Plugin tool discovery from `tools/*` with manifest-based broker policy mapping
- Local provider adapter (`providers/local_adapter.py`) with quality-threshold routing for low-stakes prompts
- Council mode with specialist perspectives and synthesized final answer
- Delegation gateway MVP (`mmctl delegate`) with git worktree isolation and patch/check/risk artifacts
- xAI + Google provider adapters (configurable and usable via `--provider`)

## Release Status

CerbiBot backend is currently part of the public technical preview stack.

The current focus is product hardening, connector expansion, and operator experience refinement rather than introducing a separate phased rewrite.
