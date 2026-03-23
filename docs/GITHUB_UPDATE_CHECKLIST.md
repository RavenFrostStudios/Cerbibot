# GitHub Update Checklist

Updated: 2026-03-21

## Goal

Use this checklist when updating the GitHub repo so the public project matches the current local state.

## Repo Basics

- push the latest local source-of-truth code
- push the updated status docs
- push the new `docs/` folder
- push `LICENSE`
- push `SECURITY.md`
- push `CONTRIBUTING.md`

## README

- confirm the current README reflects the actual product story
- add real GitHub badge links once repo owner/repo path is final
- add a docs link to `docs/README.md`
- add website link once the site is live

## Docs To Include

- `docs/README.md`
- `docs/FEATURES.md`
- `docs/WEBSITE_BRIEF.md`
- `docs/GITHUB_UPDATE_CHECKLIST.md`
- root release/status docs that are still actively maintained

## Release-State Docs

Ensure these are present and current:
- `MMY_Public_Release_Status_2026-03-18.md`
- `MMY_Canonical_Tracker.md`
- `MMY_Live_Acceptance_Report.md`
- `MMY_Local_Pentest_Report.md`

## Proof / Evidence

If publishing launch-readiness evidence, include:
- RC sweep result
- live acceptance result
- pentest result
- note that local workspace is canonical if git history was previously behind

## Optional GitHub Cleanup

- create issue templates
- create a security advisory/contact section
- add a docs label set
- add a roadmap discussion or pinned issue
- add screenshots/GIFs of dashboard pages

## Website Inputs

Before handing off to website work, provide:
- `docs/FEATURES.md`
- `docs/WEBSITE_BRIEF.md`
- current screenshots of:
  - chat
  - runs
  - settings
  - skills
  - artifacts

## Release Label

Recommended public label:
- `Public Technical Preview`

Avoid presenting the current build as a fully polished broad release unless the launch scope changes.
