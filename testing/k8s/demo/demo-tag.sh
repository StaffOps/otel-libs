#!/usr/bin/env bash
# Prints the default demo image tag (specs/demo-environment DE4): the short sha
# of the newest commit that touched an input of the demo images — the shared
# contract, the demo apps and the helper sources they build from. A commit that
# changes only docs (*.md) or other parts of the repo keeps the tag, so
# build-demo.sh and demo.sh agree without a rebuild. Exit 2 outside a git
# checkout (there is no `latest` fallback).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
root="$(git -C "$here" rev-parse --show-toplevel 2>/dev/null)" || {
  echo "demo-tag: not in a git checkout; set TAG explicitly" >&2
  exit 2
}
tag=$(git -C "$root" log -1 --format=%h -- \
  demo \
  go \
  python/demo python/staffops_otel_helper python/otel_helper python/pyproject.toml \
  nodejs/demo nodejs/packages nodejs/package.json nodejs/package-lock.json nodejs/.npmrc nodejs/tsconfig.base.json \
  dotnet/demo 'dotnet/StaffOps.*' dotnet/Directory.Build.props dotnet/Directory.Build.targets \
  ':(exclude,glob)**/*.md')
[ -n "$tag" ] || { echo "demo-tag: no commit touches the demo image inputs" >&2; exit 2; }
printf '%s\n' "$tag"
