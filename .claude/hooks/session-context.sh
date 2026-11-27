#!/bin/sh
# SessionStart: inject branch, dirty count and open spec tasks (RT3). No network.
cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null || exit 0
branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
dirty=$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')
# Historical specs: the dirs linked in the "Shipped (historical" item of specs/README.md.
shipped=$(awk '/Shipped \(historical/{f=1} f&&/^$/{f=0} f' specs/README.md | grep -oE '\]\(([a-z0-9.-]+)/\)' | sed -E 's/\]\(|\/\)//g')
open=$(for f in specs/*/tasks.md; do
  [ -f "$f" ] || continue
  d=$(basename "$(dirname "$f")")
  printf '%s\n' "$shipped" | grep -qx "$d" && continue
  grep -E '^\| [^|]+\|.*(☐|Deferred|Pending)|^- \[ \]' "$f" | head -12 | sed -E "s#^#$d: #" | cut -c1-140
done | head -45)
ctx="otel-libs session context
branch: $branch, uncommitted files: $dirty
Canonical rules: AGENTS.md. Specs: specs/README.md. Skills: change-loop, release, parity-case. Agent: spec-refuter.
Open spec tasks (≤12 per active spec):
$open"
jq -n --arg c "$ctx" '{hookSpecificOutput: {hookEventName: "SessionStart", additionalContext: $c}}'
