#!/usr/bin/env bash
# Builds the test-only OTLP receiver (testing/fakeotlp) for linux/amd64 and
# pushes it, for the soak's hung and redirect sink pods (specs/soak-simulation
# task K-hang, design K-D14): `--hang` holds every Export unanswered, and the
# default mode answers /parity-redirect-c/... with a 307. The build context is
# testing/fakeotlp of the committed HEAD, exported with `git archive`
# (uncommitted work never lands in an image), built with the builder and
# platform flags of build-images.sh.
#
# Usage: testing/k8s/build-fakeotlp.sh
#   TAG          image tag      (default: short sha of the newest commit that
#                touched testing/fakeotlp; never `latest`, which is refused)
#   REGISTRY     image prefix   (default: harbor.staffops.space/development)
#   BUILDER      buildx builder (default: multiarch)
#   MIN_FREE_GB  stop when the local Docker daemon has less free disk (default: 3)
#   PUSH         1 pushes (default) and prints the digest to record, 0 builds
#                into the builder cache only
# Image: $REGISTRY/otel-libs-fakeotlp:$TAG
# Exit: 0 done, 2 usage, 3 disk guard, 4 the registry refused authentication
# (pull or push: log in yourself and re-run; this script never logs in),
# other = build failure.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
root="$(git -C "$here" rev-parse --show-toplevel)"
TAG="${TAG:-$(git -C "$root" log -1 --format=%h -- testing/fakeotlp)}"
REGISTRY="${REGISTRY:-harbor.staffops.space/development}"
BUILDER="${BUILDER:-multiarch}"
MIN_FREE_GB="${MIN_FREE_GB:-3}"
PUSH="${PUSH:-1}"

case "$PUSH" in 0|1) ;; *) echo "build-fakeotlp: PUSH must be 0 or 1" >&2; exit 2 ;; esac
# Same rule as build-demo.sh: a valid image tag, never the mutable `latest`.
if ! [[ "$TAG" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]] || [ "$TAG" = latest ]; then
  echo "build-fakeotlp: TAG '$TAG' is not a valid image tag (or is latest)" >&2
  exit 2
fi
[[ "$MIN_FREE_GB" =~ ^[0-9]+$ ]] || { echo "build-fakeotlp: MIN_FREE_GB must be a whole number" >&2; exit 2; }

ctx="$(mktemp -d)"
log="$(mktemp)"
meta="$(mktemp)"
trap 'rm -rf "$ctx" "$log" "$meta"' EXIT
git -C "$root" archive HEAD testing/fakeotlp | tar -x -C "$ctx"
[ -f "$ctx/testing/fakeotlp/Dockerfile" ] || {
  echo "build-fakeotlp: testing/fakeotlp/Dockerfile is not in HEAD ($(git -C "$root" rev-parse --short HEAD))" >&2
  exit 1
}

# Free disk of the local Docker daemon (its VM on macOS), in GB; as in
# build-demo.sh, it does not measure a docker-container builder's storage.
free=$(docker run --rm alpine:3.20 df -k / | awk 'NR==2 {printf "%d", $4 / 1048576}')
if ! [[ "$free" =~ ^[0-9]+$ ]]; then
  echo "build-fakeotlp: could not read the free Docker disk; stopping" >&2
  exit 3
fi
if [ "$free" -lt "$MIN_FREE_GB" ]; then
  echo "build-fakeotlp: only ${free} GB free on the Docker disk (< ${MIN_FREE_GB}); stopping" >&2
  exit 3
fi

output=(--push --metadata-file "$meta")
[ "$PUSH" = 1 ] || output=()

image="$REGISTRY/otel-libs-fakeotlp:$TAG"
echo "build-fakeotlp: $image (HEAD $(git -C "$root" rev-parse --short HEAD)), ${free} GB free"
if ! docker buildx build --builder "$BUILDER" --platform linux/amd64 \
    --provenance=false --sbom=false ${output[@]+"${output[@]}"} \
    -t "$image" "$ctx/testing/fakeotlp" 2>&1 | tee "$log"; then
  # A refused authentication ends the run with its own code; never retry with a login.
  if grep -qiE 'unauthorized|authentication required|denied: requested access|insufficient_scope' "$log"; then
    echo "build-fakeotlp: registry refused authentication (pull or push) while building $image; log in yourself and re-run" >&2
    exit 4
  fi
  echo "build-fakeotlp: build failed" >&2
  exit 1
fi
if [ "$PUSH" = 1 ]; then
  digest=$(sed -n 's/.*"containerimage\.digest": *"\(sha256:[0-9a-f]\{64\}\)".*/\1/p' "$meta" | head -1)
  [ -n "$digest" ] || { echo "build-fakeotlp: pushed, but no digest in the build metadata" >&2; exit 1; }
  echo "build-fakeotlp: pushed $image@$digest (record the digest; reference the image by it)"
else
  echo "build-fakeotlp: built $image into the builder cache (not pushed)"
fi
