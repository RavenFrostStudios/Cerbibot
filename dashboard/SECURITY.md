# Security Policy

## Scope

This repository is the dashboard/UI layer for CerbiBot.

Security issues are especially important when they affect:
- auth token handling
- admin-password-gated actions
- artifact export flows
- provider credential handling
- cross-project data visibility
- run/event stream exposure

## Reporting

Until a dedicated public security contact is published, report security issues
privately to RavenFrost Studios or the maintainer who provided the build.

Do not open public issues for:
- token exposure
- auth bypass
- export bypass
- project isolation failures
- sensitive data leakage in UI/API flows

## Include In Reports

- affected version or commit if known
- environment
- steps to reproduce
- expected vs actual behavior
- impact assessment
- redacted screenshots or logs where useful

## Safe Testing

Only test against systems and environments you own or are explicitly authorized
to assess.
