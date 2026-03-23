#!/usr/bin/env bash
set -euo pipefail

# Quick helper to commit and push current branch updates.
# Usage:
#   scripts/push_updates.sh "Your commit message"
#   scripts/push_updates.sh -m "Your commit message"
#   scripts/push_updates.sh --amend-message "Your commit message"

msg=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    -m|--message|--amend-message)
      shift
      msg="${1:-}"
      ;;
    -h|--help)
      cat <<USAGE
Usage: scripts/push_updates.sh [options] [message]

Options:
  -m, --message <msg>   Commit message
  -h, --help            Show this help

Examples:
  scripts/push_updates.sh "Add task 09"
  scripts/push_updates.sh -m "Update CI workflows"
USAGE
      exit 0
      ;;
    *)
      if [[ -z "$msg" ]]; then
        msg="$1"
      fi
      ;;
  esac
  shift || true
done

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "Not inside a git repository." >&2
  exit 1
fi

current_branch="$(git rev-parse --abbrev-ref HEAD)"
if [[ -z "$current_branch" || "$current_branch" == "HEAD" ]]; then
  echo "Detached HEAD; checkout a branch before pushing." >&2
  exit 1
fi

if ! git remote get-url origin >/dev/null 2>&1; then
  echo "Remote 'origin' is not configured." >&2
  exit 1
fi

if [[ -z "$msg" ]]; then
  msg="Update $(date -u +'%Y-%m-%d %H:%M:%SZ')"
fi

if [[ -z "$(git status --porcelain)" ]]; then
  echo "No local changes to commit."
  echo "Attempting push of current branch anyway..."
  git push -u origin "$current_branch"
  echo "Push completed."
  exit 0
fi

echo "Staging changes..."
git add -A

echo "Committing..."
if git commit -m "$msg"; then
  echo "Commit created."
else
  echo "No commit created (possibly nothing new after staging)."
fi

echo "Pushing to origin/$current_branch ..."
git push -u origin "$current_branch"

echo "Done."
