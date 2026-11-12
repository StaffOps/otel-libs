#!/bin/sh
# PreToolUse(Bash): remind to update docs, only when the command runs `git commit` (RT1).
cmd=$(jq -r '.tool_input.command // ""' | tr '\n\\' '  ' | sed -E "s/'[^']*'|\"[^\"]*\"//g")
printf '%s\n' "$cmd" | grep -Eq '(^|[;&|(/[:space:]])git([[:space:]]+(-C|-c|--git-dir|--work-tree)[[:space:]]+[^[:space:]]+|[[:space:]]+-[^[:space:]]+)*[[:space:]]+commit([[:space:]]|$)' || exit 0
jq -n '{hookSpecificOutput: {hookEventName: "PreToolUse", additionalContext: "Pre-commit reminder (otel-libs): docs must match the change — root README.md (env table), CHANGELOG.md (Unreleased), per-language HOW-TO.md; spec corrections dated (specs/README.md). Run tools/spec_lint.py if specs changed."}}'
