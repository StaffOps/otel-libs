#!/bin/sh
# PreToolUse(Bash): ask a human before commands that delete Docker state or
# rewrite/remove published git state (RT2). Asks, never denies (T-D2).
# Fails closed (asks) when jq is missing.
command -v jq >/dev/null 2>&1 || { printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"ask","permissionDecisionReason":"otel-libs guard: jq missing, cannot inspect the command"}}'; exit 0; }
cmd=$(jq -r '.tool_input.command // ""')
# Join continuation lines, then strip quoted strings in one left-to-right pass
# so `grep "prune"` or a commit message doesn't trigger.
plain=$(printf '%s' "$cmd" | tr '\n\\' '  ' | sed -E "s/'[^']*'|\"[^\"]*\"//g")
reason=""
check() { printf '%s\n' "$plain" | grep -Eq "$1" && reason="$2"; }
B='(^|[;&|(`/[:space:]])'                                        # command boundary (incl. /usr/bin/docker)
O='([[:space:]]+-[^[:space:]]+([[:space:]]+[^-[:space:]][^[:space:]]*)?)*'  # global options (-H x, -C dir)
A='([[:space:]]+[^;&|]*)?'                                        # args up to the next command separator
check "${B}docker${O}([[:space:]]+[a-z-]+)*[[:space:]]+prune([[:space:]]|$)" "docker prune removes shared Docker state"
check "${B}docker${O}[[:space:]]+(volume[[:space:]]+rm|rmi|image[[:space:]]+rm)([[:space:]]|$)" "removes Docker volumes/images"
check "${B}(docker${O}[[:space:]]+compose|docker-compose)${A}[[:space:]]down${A}[[:space:]](-v|--volumes(=[^[:space:]]*)?|--rmi)([[:space:]]|$)" "compose down -v/--rmi deletes volumes or images"
check "${B}git${O}[[:space:]]+push${A}[[:space:]](-[a-zA-Z]*[fd][a-zA-Z]*|--force(-with-lease)?(=[^[:space:]]*)?|--delete|--mirror|--prune|\+[^[:space:]]+|:[^[:space:]]*)([[:space:]]|$)" "git push that overwrites or deletes remote refs"
check "${B}git${O}[[:space:]]+tag${A}[[:space:]](-[a-zA-Z]*d[a-zA-Z]*|--delete)([[:space:]]|$)" "deletes git tags"
check "${B}git${O}[[:space:]]+(filter-repo|filter-branch)([[:space:]]|$)" "rewrites git history"
check "${B}git${O}[[:space:]]+reset${A}[[:space:]]--hard([[:space:]]|$)" "discards working-tree changes"
check "${B}git${O}[[:space:]]+clean${A}[[:space:]]-[a-zA-Z]*f" "deletes untracked files"
check "${B}git${O}[[:space:]]+branch${A}[[:space:]](-D|--delete[[:space:]]+--force)([[:space:]]|$)" "force-deletes a branch"
[ -n "$reason" ] || exit 0
jq -n --arg r "otel-libs guard (specs/repo-tooling RT2): $reason. Confirm only if the owner asked for it." \
  '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: "ask", permissionDecisionReason: $r}}'
