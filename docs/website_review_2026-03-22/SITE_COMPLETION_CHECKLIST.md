# CerbiBot Site Completion Checklist

## Highest Priority

- Replace placeholder or non-functional CTA buttons with real links.
- Add a real `Docs` section with visible links to GitHub repos and setup material.
- Add a real `Launch` section so the `#launch` nav item points somewhere valid.
- Fix nav-to-section mismatches.
- Align site legal wording with the Apache-2.0 repo licensing decision.

## Navigation

- Ensure `#features` points to the features section.
- Ensure `#security` points to an actual security-oriented section.
- Add an element with `id="docs"`.
- Add an element with `id="launch"`.
- If `Security` remains in the nav, the section title should read like security, trust, or controls instead of `Why It Exists`.

## CTA Wiring

- `Run Locally` should link to install/setup instructions, not stay as a plain button.
- `Explore Features` should scroll to `#features`.
- `View Docs` should scroll to `#docs` or open the docs page directly.
- Footer nav items should all resolve to real sections or routes.

## Content

- Keep `CerbiBot` as the public product name throughout.
- Keep `Public Technical Preview` visible.
- Add a short sentence explaining what that means:
  - stable core product
  - still expanding connectors and polish
- Add GitHub links for:
  - `multi-mind-orchestrator`
  - `ai-orchestrator-dashboard`
- Add a short install block with:
  - backend startup
  - dashboard startup
  - one-command stack option if you want to expose it

## Trust

- Add a small validation block:
  - live acceptance: pass
  - local pentest: pass
  - dashboard build/typecheck: pass
- Add a brief security controls list:
  - bearer auth
  - admin-password-gated actions
  - audit visibility
  - project isolation
- Replace `All rights reserved` in the footer if you want the site to match the Apache-2.0 public repos.

## Visual/Product Proof

- Add at least one real product screenshot.
- Prefer a screenshot of the dashboard over only the stylized terminal card.
- If possible, add one short GIF showing:
  - run creation
  - run visibility
  - provider/settings control

## Metadata

- Add Open Graph title and description.
- Add Open Graph image.
- Add Twitter/X card tags.
- Add canonical URL.
- Make sure the preview image is not just the small logo unless intentional.

## Suggested Finish Order

1. Fix buttons and nav targets.
2. Add Docs and Launch sections.
3. Add GitHub and setup links.
4. Add validation/security trust block.
5. Add screenshots or a short product GIF.
6. Clean up footer/legal wording.
