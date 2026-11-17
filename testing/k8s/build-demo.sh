#!/usr/bin/env bash
# Builds the demo apps (worker, api, backend per language; specs/demo-environment
# DE4) for linux/amd64 and pushes them, to run on Kubernetes with
# testing/k8s/demo/demo.sh apply. The build context is the repository root of
# the committed HEAD exported with `git archive` (uncommitted work never lands
# in an image; DE-D3: the demo Dockerfiles need demo/proto and demo/sql next to
# the language tree). Each Dockerfile has a Dockerfile.dockerignore next to it
# that limits the context to what it copies:
#   go      go/demo/<app>/Dockerfile
#   python  python/demo/<app>/Dockerfile
#   node    nodejs/demo/<app>/Dockerfile
#   dotnet  dotnet/demo/<App>/Dockerfile   (Worker, Api, Backend)
# One image at a time; the local Docker disk is checked before each build.
#
# Usage: testing/k8s/build-demo.sh [lang[/app] ...]   (default: all twelve)
#   e.g.  testing/k8s/build-demo.sh go python/api
#   TAG          image tag      (default: demo/demo-tag.sh, the newest commit that
#                touched an image input; never `latest`, which is refused)
#   REGISTRY     image prefix   (default: harbor.staffops.space/development)
#   BUILDER      buildx builder (default: multiarch)
#   MIN_FREE_GB  stop when the local Docker daemon has less free disk (default: 3)
#   PUSH         1 pushes (default), 0 builds into the builder cache only
# Images: $REGISTRY/otel-libs-demo-<lang>-<app>:$TAG
# Exit: 0 done, 2 usage, 3 disk guard, 4 the registry refused authentication
# (pull or push: log in yourself and re-run; this script never logs in),
# other = build failure.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
root="$(git -C "$here" rev-parse --show-toplevel)"
TAG="${TAG:-$("$here/demo/demo-tag.sh")}"
REGISTRY="${REGISTRY:-harbor.staffops.space/development}"
BUILDER="${BUILDER:-multiarch}"
MIN_FREE_GB="${MIN_FREE_GB:-3}"
PUSH="${PUSH:-1}"
apps=(worker api backend)

# dockerfile_for <lang> <app>  ->  repository-relative Dockerfile path
dockerfile_for() {
  case "$1/$2" in
    go/worker|go/api|go/backend) printf 'go/demo/%s/Dockerfile' "$2" ;;
    python/worker|python/api|python/backend) printf 'python/demo/%s/Dockerfile' "$2" ;;
    node/worker|node/api|node/backend) printf 'nodejs/demo/%s/Dockerfile' "$2" ;;
    dotnet/worker) printf 'dotnet/demo/Worker/Dockerfile' ;;
    dotnet/api) printf 'dotnet/demo/Api/Dockerfile' ;;
    dotnet/backend) printf 'dotnet/demo/Backend/Dockerfile' ;;
    *) return 1 ;;
  esac
}

targets=()
if [ $# -eq 0 ]; then
  set -- go python node dotnet
fi
for arg in "$@"; do
  case "$arg" in
    */*) targets+=("$arg") ;;
    *) for app in "${apps[@]}"; do targets+=("$arg/$app"); done ;;
  esac
done
# Validate every target before exporting or building anything.
for target in "${targets[@]}"; do
  dockerfile_for "${target%/*}" "${target#*/}" >/dev/null || {
    echo "build-demo: unknown target '$target' (lang: go python node dotnet; app: worker api backend)" >&2
    exit 2
  }
done
case "$PUSH" in 0|1) ;; *) echo "build-demo: PUSH must be 0 or 1" >&2; exit 2 ;; esac
# Same rule as demo.sh: a valid image tag, never the mutable `latest`.
if ! [[ "$TAG" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]] || [ "$TAG" = latest ]; then
  echo "build-demo: TAG '$TAG' is not a valid image tag (or is latest)" >&2
  exit 2
fi
[[ "$MIN_FREE_GB" =~ ^[0-9]+$ ]] || { echo "build-demo: MIN_FREE_GB must be a whole number" >&2; exit 2; }

ctx="$(mktemp -d)"
log="$(mktemp)"
trap 'rm -rf "$ctx" "$log"' EXIT
git -C "$root" archive HEAD | tar -x -C "$ctx"

# Free disk of the local Docker daemon (its VM on macOS), in GB. It does not
# measure a remote or docker-container buildx builder's storage: with such a
# builder this guard only protects the local daemon.
free_gb() {
  docker run --rm alpine:3.20 df -k / | awk 'NR==2 {printf "%d", $4 / 1048576}'
}

output=(--push)
[ "$PUSH" = 1 ] || output=()

built=0
for target in "${targets[@]}"; do
  lang=${target%/*}
  app=${target#*/}
  df="$(dockerfile_for "$lang" "$app")"
  [ -f "$ctx/$df" ] || { echo "build-demo: $df is not in HEAD ($(git -C "$root" rev-parse --short HEAD))" >&2; exit 1; }

  free=$(free_gb)
  if ! [[ "$free" =~ ^[0-9]+$ ]]; then
    echo "build-demo: could not read the free Docker disk; stopping before $target" >&2
    exit 3
  fi
  if [ "$free" -lt "$MIN_FREE_GB" ]; then
    echo "build-demo: only ${free} GB free on the Docker disk (< ${MIN_FREE_GB}); stopping before $target" >&2
    exit 3
  fi

  image="$REGISTRY/otel-libs-demo-$lang-$app:$TAG"
  echo "build-demo: [$((built + 1))/${#targets[@]}] $image (${df}), ${free} GB free"
  if ! docker buildx build --builder "$BUILDER" --platform linux/amd64 \
      --provenance=false --sbom=false ${output[@]+"${output[@]}"} \
      -f "$ctx/$df" -t "$image" "$ctx" 2>&1 | tee "$log"; then
    # A refused authentication ends the run with its own code; never retry with a login.
    if grep -qiE 'unauthorized|authentication required|denied: requested access|insufficient_scope' "$log"; then
      echo "build-demo: registry refused authentication (pull or push) while building $image; log in yourself and re-run" >&2
      exit 4
    fi
    echo "build-demo: build of $target failed" >&2
    exit 1
  fi
  built=$((built + 1))
done
echo "build-demo: $built image(s) built$([ "$PUSH" = 1 ] && echo ' and pushed'), tag $TAG"
