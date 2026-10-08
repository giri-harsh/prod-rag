#!/usr/bin/env bash
# Fails (non-zero) if any commit mentions an AI assistant in author/committer/message, or if assistant config files are tracked.
#   bash scripts/check_attribution.sh
cd "$(dirname "$0")/.." || exit 2
status=0
robot=$(printf '\xf0\x9f\xa4\x96')
if git log --all --format='%an %ae %cn %ce %B' | grep -qiE "claude|anthropic|${robot}"; then
  echo "check_attribution FAILED: commit history mentions claude/anthropic/robot-emoji in author, committer or message" >&2
  git log --all --format='%h %an <%ae> | %s' | grep -iE "claude|anthropic|${robot}" | head -5 >&2
  status=1
fi
if git ls-files | grep -qE '^(\.claude/|CLAUDE\.md$)'; then
  echo "check_attribution FAILED: .claude/ or CLAUDE.md is tracked (git rm -r --cached it and add it to .gitignore)" >&2
  status=1
fi
[ "$status" -eq 0 ] && echo "check_attribution OK: history and tracked files are clean"
exit "$status"
