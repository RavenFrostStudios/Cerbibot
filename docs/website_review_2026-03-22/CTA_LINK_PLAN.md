# CTA And Nav Plan

## Primary Buttons

### Run Locally

Use a real link.

Best target options:
- `/docs`
- `#docs`
- direct GitHub setup documentation

If you keep the user on the homepage, point to `#docs`.

### Explore Features

Target:
- `#features`

### View Docs

Target:
- `#docs`

## Nav Items

### Features

Target:
- `#features`

### Security

Either:
- rename the section to `Security And Control`

Or:
- rename the nav item to `Why CerbiBot`

Current homepage copy reads more like product rationale than security.

### Docs

Target:
- `#docs`

### Launch

Target:
- `#launch`

## Suggested Section IDs

- `id="features"`
- `id="security"`
- `id="docs"`
- `id="launch"`

## Implementation Note

Avoid using plain `<button>` elements for navigation-only actions unless they are wired with router or scroll handlers. If the action is just navigation, use links.
