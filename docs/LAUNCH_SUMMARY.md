# Launch Summary

Updated: 2026-03-21

## Current Position

CerbiBot is launchable now as a public technical preview.

Current release evidence:
- live acceptance: PASS (`12/14`, `2` expected skips)
- local pentest: PASS (`10/10`)
- dashboard TypeScript build trust issue resolved
- delegate-focused tests passing
- heartbeat coverage strengthened

## What Is No Longer Blocking Launch

- stale acceptance evidence
- stale pentest evidence
- dashboard type-check bypass
- missing repo trust files
- uncertainty around heartbeat coverage
- uncertainty around delegate backend coverage

## Remaining Optional Polish

- final public launch memo / release notes
- FastAPI lifespan migration to remove `@app.on_event(...)` deprecation warnings
- screenshots and website assets
- broader docs cleanup and packaging polish

## Recommended Public Framing

Launch as:
- `Public Technical Preview`

Lead with:
- orchestration depth
- local control
- governed skills
- project isolation
- audit/artifacts

Do not lead with:
- broad chat connector coverage
- consumer-ready ecosystem breadth

## Best Immediate Next Steps

1. Update GitHub with the current local code and docs.
2. Build the website using `WEBSITE_BRIEF.md` and `FEATURES.md`.
3. Prepare a short launch note.
4. Decide whether connector expansion is a post-launch roadmap item or a revised launch-scope item.
