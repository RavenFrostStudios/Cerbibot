# Security Policy

## Scope

This project includes:
- a local/self-hosted orchestration runtime
- provider API key handling
- artifact export and storage
- memory, audit, and skill execution surfaces

Treat security issues affecting auth, secrets, encryption, audit integrity,
remote exposure, data export, or tool execution as high priority.

## Reporting

Until a dedicated reporting channel is published, report security issues
privately to the project owner or maintainer who provided this build.

Do not open public issues for:
- credential exposure
- auth bypass
- remote execution bugs
- encryption failures
- cross-project data leakage
- artifact export bypass

Include:
- affected version or commit if known
- environment
- steps to reproduce
- expected vs actual behavior
- impact assessment
- redacted logs or request/response samples

## Handling Expectations

Current target triage times:
- Critical: same day
- High: within 24 hours
- Medium: within 3 business days
- Low: next scheduled maintenance cycle

## Safe Testing

Only test against systems you own or are explicitly authorized to assess.
Do not target third-party providers, accounts, or infrastructure without
permission.
