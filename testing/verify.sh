#!/usr/bin/env bash
# Local mirror of the CI checks (specs/repo-tooling RT6). Docker only, one
# step at a time, one summary at the end.
#
#   testing/verify.sh                 # every step
#   testing/verify.sh --quick         # skip e2e and the extra Python legs
#   testing/verify.sh --only <step>   # one step (repeatable)
#   testing/verify.sh --list          # step names
#
# Steps: go, go-ext, py-3.10-lower, py-3.13-lower, py-3.13-latest, node, e2e,
# mkdocs, spec-lint, trace, index, tools-tests. `node` runs the Node.js
# workspace, parity runner included, on node:${NODE_VERSION:-24}-slim (CI
# covers 22.15.0/22/24/26).
#
# Disk safety: this script never prunes, never runs `compose down -v` and never
# removes volumes or images. At exit it stops only the compose services it
# started itself (fakeotlp-go, fakeotlp-py, fakeotlp-node, collector: the ones
# not running when it began). The e2e step is testing/e2e/smoke.sh, which
# applies the same rule to the Collector.
set -uo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
compose=(docker compose -f "$root/testing/docker-compose.yml")
# Same mkdocs-material version as .github/workflows/docs.yml (the deployed site).
MKDOCS_IMAGE="squidfunk/mkdocs-material:9.5.39"
TOOLS_IMAGE="python:3.12-slim"

ALL_STEPS=(go go-ext py-3.10-lower py-3.13-lower py-3.13-latest node e2e mkdocs spec-lint trace index tools-tests)
QUICK_SKIP=" e2e py-3.10-lower py-3.13-lower "

usage() { sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; }

quick=0
only=()
while [ $# -gt 0 ]; do
  case "$1" in
    --quick) quick=1 ;;
    --only)
      [ $# -ge 2 ] || { echo "--only needs a step name" >&2; exit 2; }
      only+=("$2"); shift ;;
    --list) printf '%s\n' "${ALL_STEPS[@]}"; exit 0 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

for s in ${only[@]+"${only[@]}"}; do
  case " ${ALL_STEPS[*]} " in *" $s "*) ;; *) echo "unknown step: $s (see --list)" >&2; exit 2 ;; esac
done

# ── service bookkeeping: stop only what this run started ─────────────────
# The collector is under the e2e profile; `ps` lists it only with --profile.
running_services() {
  "${compose[@]}" --profile e2e ps --status running --services 2>/dev/null \
    | grep -E '^(fakeotlp-go|fakeotlp-py|fakeotlp-node|collector)$' || true
}
preexisting=" $(running_services | tr '\n' ' ') "
cleanup() {
  local started=()
  for svc in $(running_services); do
    case "$preexisting" in *" $svc "*) ;; *) started+=("$svc") ;; esac
  done
  if [ ${#started[@]} -gt 0 ]; then
    echo "verify: stopping ${started[*]} (started by this run)"
    "${compose[@]}" --profile e2e stop "${started[@]}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# ── steps ────────────────────────────────────────────────────────────────
step_go() { "${compose[@]}" run --rm go-tests; }

step_go_ext() {
  # Same module list as the CI go-test job (ext build/vet/test, examples build/vet).
  "${compose[@]}" run --rm go-tests sh -ec '
    for d in ext/otelaws ext/otelredis ext/otelsql; do
      echo "== $d"; (cd "$d" && go build ./... && go vet ./... && go test -count=1 ./...)
    done
    for d in example/go-api example/go-backend example/go-process example/go-custom-signals ../testing/e2e/go-smoke; do
      echo "== $d"; (cd "$d" && go build -o /dev/null ./... && go vet ./...)
    done'
}

step_python() { PY_VERSION="$1" PY_DEPS="$2" "${compose[@]}" run --rm python-tests; }

step_node() {
  [ -n "${NODE_TESTS_OFFLINE:-}" ] && echo "WARNING: NODE_TESTS_OFFLINE is set: the api-range install test is skipped (CI always runs it)" >&2
  # Rebuild the receiver image first: a stale otel-libs/fakeotlp:local lacks the
  # fakeotlp / fakeotlp-tls SANs the Node legs dial (design D-JS14). A running
  # fakeotlp-node keeps its old container; restart it to pick the new image up.
  "${compose[@]}" build fakeotlp-node || return 1
  "${compose[@]}" run --rm node-tests
}

step_e2e() { "$root/testing/e2e/smoke.sh"; }

step_mkdocs() {
  # Read-only mount; the site is written inside the container only.
  docker run --rm -v "$root:/docs:ro" -w /docs "$MKDOCS_IMAGE" build --strict --site-dir /tmp/site
}

tools_run() {
  docker run --rm -e PYTHONDONTWRITEBYTECODE=1 -v "$root:/repo:ro" -w /repo "$TOOLS_IMAGE" "$@"
}

step_tools_tests() {
  # The hook tests need jq and git, which the slim image lacks: install them in
  # this throwaway container, and REQUIRE_HOOK_TESTS=1 turns a skip into a failure.
  docker run --rm -e PYTHONDONTWRITEBYTECODE=1 -e REQUIRE_HOOK_TESTS=1 -e DEBIAN_FRONTEND=noninteractive \
    -v "$root:/repo:ro" -w /repo "$TOOLS_IMAGE" sh -ec '
      apt-get update -qq >/dev/null
      apt-get install -y -qq --no-install-recommends jq git >/dev/null
      git config --global --add safe.directory /repo
      python -m unittest discover -s tools/tests'
}

run_step() {
  case "$1" in
    go) step_go ;;
    go-ext) step_go_ext ;;
    py-3.10-lower) step_python 3.10 lower ;;
    py-3.13-lower) step_python 3.13 lower ;;
    py-3.13-latest) step_python 3.13 latest ;;
    node) step_node ;;
    e2e) step_e2e ;;
    mkdocs) step_mkdocs ;;
    spec-lint) tools_run python tools/spec_lint.py ;;
    trace) tools_run python tools/trace_matrix.py --check ;;
    index) tools_run python tools/regen_index.py --check ;;
    tools-tests) step_tools_tests ;;
  esac
}

selected=()
if [ ${#only[@]} -gt 0 ]; then
  selected=("${only[@]}")
else
  for s in "${ALL_STEPS[@]}"; do
    if [ "$quick" = 1 ] && [[ "$QUICK_SKIP" == *" $s "* ]]; then continue; fi
    selected+=("$s")
  done
fi

results=()
failed=0
for s in "${selected[@]}"; do
  echo
  echo "════ verify: $s ════"
  start=$SECONDS
  if run_step "$s"; then r=PASS; else r=FAIL; failed=$((failed + 1)); fi
  results+=("$(printf '%-15s %-4s %5ss' "$s" "$r" "$((SECONDS - start))")")
done
if [ "$quick" = 1 ] && [ ${#only[@]} -eq 0 ]; then
  for s in $QUICK_SKIP; do results+=("$(printf '%-15s %-4s' "$s" "SKIP (--quick)")"); done
fi

echo
echo "════ verify summary ════"
printf '%-15s %-4s %6s\n' STEP RESULT TIME
printf '%s\n' "${results[@]}"
echo "$(( ${#selected[@]} - failed ))/${#selected[@]} step(s) passed"
[ "$failed" -eq 0 ]
