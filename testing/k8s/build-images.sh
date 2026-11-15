#!/usr/bin/env bash
# Builds the e2e smoke images (go, python, node, dotnet) for linux/amd64 and
# pushes them, to run later as Kubernetes Jobs. The build context is the
# committed HEAD exported with `git archive` (uncommitted work never lands in
# an image) plus this directory's Dockerfiles and the Python smoke program.
#
# Usage: testing/k8s/build-images.sh [lang ...]   (default: all four)
#   TAG       image tag      (default: short HEAD sha); :latest is pushed too
#   REGISTRY  image prefix   (default: harbor.staffops.space/development)
#   BUILDER   buildx builder (default: multiarch)
# Each image runs the smoke program once with the standard OTel variables
# (OTEL_EXPORTER_OTLP_ENDPOINT, OTEL_EXPORTER_OTLP_PROTOCOL, OTEL_SERVICE_NAME,
# OTEL_RESOURCE_ATTRIBUTES, OTEL_METRIC_EXPORT_INTERVAL).
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
root="$(git -C "$here" rev-parse --show-toplevel)"
TAG="${TAG:-$(git -C "$root" rev-parse --short HEAD)}"
REGISTRY="${REGISTRY:-harbor.staffops.space/development}"
BUILDER="${BUILDER:-multiarch}"
langs=("$@")
[ ${#langs[@]} -gt 0 ] || langs=(go python node dotnet)

ctx="$(mktemp -d)"
trap 'rm -rf "$ctx"' EXIT
git -C "$root" archive HEAD | tar -x -C "$ctx"
# Only this directory comes from the working tree (it may not be committed yet).
mkdir -p "$ctx/testing/k8s"
rm -rf "$ctx/testing/k8s/images"
cp -R "$here/images" "$ctx/testing/k8s/images"

for lang in "${langs[@]}"; do
  df="$ctx/testing/k8s/images/$lang.Dockerfile"
  [ -f "$df" ] || { echo "build-images: no Dockerfile for '$lang'" >&2; exit 1; }
  image="$REGISTRY/otel-libs-smoke-$lang"
  echo "build-images: $image:$TAG (+ :latest)"
  docker buildx build --builder "$BUILDER" --platform linux/amd64 \
    --provenance=false --sbom=false \
    -f "$df" -t "$image:$TAG" -t "$image:latest" --push "$ctx"
done
