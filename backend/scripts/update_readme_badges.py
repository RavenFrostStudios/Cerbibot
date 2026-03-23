#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"

BADGE_PATTERN = re.compile(r"https://github\.com/<OWNER>/<REPO>/actions/workflows/([a-zA-Z0-9_.-]+)")


def _run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "command failed").strip())
    return (proc.stdout or "").strip()


def _parse_owner_repo(url: str) -> tuple[str, str] | None:
    url = url.strip()
    # SSH: git@github.com:owner/repo.git
    m = re.match(r"^git@github\.com:([^/]+)/(.+?)(?:\.git)?$", url)
    if m:
        return m.group(1), m.group(2)
    # HTTPS: https://github.com/owner/repo(.git)
    m = re.match(r"^https://github\.com/([^/]+)/(.+?)(?:\.git)?/?$", url)
    if m:
        return m.group(1), m.group(2)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Fill README CI badge placeholders from git origin.")
    parser.add_argument("--readme", default=str(README), help="Path to README file")
    parser.add_argument("--origin-url", default="", help="Override remote URL instead of reading git origin")
    parser.add_argument("--dry-run", action="store_true", help="Print diff-style summary without writing")
    args = parser.parse_args()

    readme_path = Path(args.readme)
    if not readme_path.exists():
        raise SystemExit(f"README not found: {readme_path}")

    origin_url = args.origin_url.strip()
    if not origin_url:
        try:
            origin_url = _run(["git", "remote", "get-url", "origin"])
        except Exception as exc:
            raise SystemExit(f"Unable to read git origin. Set it first or pass --origin-url. Error: {exc}")

    parsed = _parse_owner_repo(origin_url)
    if not parsed:
        raise SystemExit(f"Unsupported origin URL format: {origin_url}")
    owner, repo = parsed

    original = readme_path.read_text(encoding="utf-8")
    replaced = original.replace("<OWNER>", owner).replace("<REPO>", repo)

    if replaced == original:
        print("No placeholder badges found or already populated.")
        return 0

    if args.dry_run:
        replaced_count = original.count("<OWNER>") + original.count("<REPO>")
        print(f"Would replace placeholders in {readme_path} for {owner}/{repo} (tokens touched: {replaced_count}).")
        return 0

    readme_path.write_text(replaced, encoding="utf-8")
    print(f"Updated {readme_path} badges for {owner}/{repo}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
