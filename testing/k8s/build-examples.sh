#!/usr/bin/env bash
# Builds the example apps (api, backend, process per language) for linux/amd64
# and pushes them, to run on Kubernetes with testing/k8s/examples/examples.sh.
# The build context is the committed HEAD exported with `git archive`
# (uncommitted work never lands in an image). Every image uses the example's
# own Dockerfile, which builds the helper from that source tree:
#   go      go/example/go-<app>/Dockerfile          context go/
#   python  python/example/python-<app>/Dockerfile  context python/
#   node    nodejs/example/node-<app>/Dockerfile    context nodejs/ (HELPER_SOURCE=workspace)
#   dotnet  dotnet/example/dotnet-<app>/Dockerfile  context dotnet/
#
# Usage: testing/k8s/build-examples.sh [lang[/app] ...]   (default: all twelve)
#   e.g.  testing/k8s/build-examples.sh go python/api
#   TAG       image tag      (default: short HEAD sha); :latest is pushed too
#   REGISTRY  image prefix   (default: harbor.staffops.space/development)
#   BUILDER   buildx builder (default: multiarch)
#   MIN_FREE_GB  stop when the local Docker daemon has less free disk (default: 3)
# Images: $REGISTRY/otel-libs-example-<lang>-<app>:$TAG
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
root="$(git -C "$here" rev-parse --show-toplevel)"
TAG="${TAG:-$(git -C "$root" rev-parse --short HEAD)}"
REGISTRY="${REGISTRY:-harbor.staffops.space/development}"
BUILDER="${BUILDER:-multiarch}"
MIN_FREE_GB="${MIN_FREE_GB:-3}"
apps=(api backend process)

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

ctx="$(mktemp -d)"
trap 'rm -rf "$ctx"' EXIT
git -C "$root" archive HEAD | tar -x -C "$ctx"

# Free disk of the local Docker daemon (its VM on macOS), in GB. It does not
# measure a remote or docker-container buildx builder's storage: with such a
# builder this guard only protects the local daemon.
free_gb() {
  docker run --rm alpine:3.20 df -k / | awk 'NR==2 {printf "%d", $4 / 1048576}'
}

for target in "${targets[@]}"; do
  lang=${target%/*}
  app=${target#*/}
  build_args=()
  case "$lang" in
    go)     dir=go;     prefix=go ;;
    python) dir=python; prefix=python ;;
    node)   dir=nodejs; prefix=node; build_args=(--build-arg HELPER_SOURCE=workspace) ;;
    dotnet) dir=dotnet; prefix=dotnet ;;
    *) echo "build-examples: unknown language '$lang'" >&2; exit 2 ;;
  esac
  df="$ctx/$dir/example/$prefix-$app/Dockerfile"
  [ -f "$df" ] || { echo "build-examples: no Dockerfile at ${df#"$ctx"/}" >&2; exit 1; }

  free=$(free_gb)
  if [ "$free" -lt "$MIN_FREE_GB" ]; then
    echo "build-examples: only ${free} GB free on the Docker disk (< ${MIN_FREE_GB}); stopping before $target" >&2
    exit 3
  fi

  image="$REGISTRY/otel-libs-example-$lang-$app"
  echo "build-examples: $image:$TAG (+ :latest), ${free} GB free"
  docker buildx build --builder "$BUILDER" --platform linux/amd64 \
    --provenance=false --sbom=false ${build_args[@]+"${build_args[@]}"} \
    -f "$df" -t "$image:$TAG" -t "$image:latest" --push "$ctx/$dir"
done
