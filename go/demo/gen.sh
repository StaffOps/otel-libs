#!/usr/bin/env bash
# Regenerates the committed Go stubs of the demo contract
# (demo/proto/otellibs/demo/v1/demo.proto -> go/demo/gen/) inside Docker; no
# host toolchain is used (specs/demo-environment DE-D3, task DE1).
#
# Usage: go/demo/gen.sh [write|check]
#   write (default)  regenerate into go/demo/gen
#   check            regenerate into a scratch directory inside the container
#                    and fail on any difference from the committed stubs; then
#                    check that go/demo/go.mod is tidy and that the module
#                    builds and vets. CI runs this mode.
# Env:   GO_IMAGE (golang:1.26-alpine, the go.mod floor)
#
# Pinned generators (bump them together and regenerate):
#   protoc v36.2 (release zip, sha256 per architecture below),
#   protoc-gen-go v1.36.12 (= the google.golang.org/protobuf runtime in go.mod),
#   protoc-gen-go-grpc v1.6.2.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/../.." && pwd)"
mode=${1:-write}
image=${GO_IMAGE:-golang:1.26-alpine}

case "$mode" in
  write) out_mount=(-v "$here/gen:/out") ;;
  check) out_mount=() ;;
  *) echo "usage: $0 [write|check]" >&2; exit 2 ;;
esac

docker run --rm \
  --user "$(id -u):$(id -g)" \
  --tmpfs /tmp:rw,exec,size=4g \
  -e HOME=/tmp -e GOPATH=/tmp/go -e GOCACHE=/tmp/gocache \
  -e MODE="$mode" \
  -v "$root:/src:ro" \
  ${out_mount[@]+"${out_mount[@]}"} \
  "$image" sh -euc '
    protoc_version=36.2
    case "$(uname -m)" in
      x86_64)  arch=x86_64;  sum=121f6c7afe1d4d0e3ea6aab9432038599250134cbf4474cb1167d2c7decd4278 ;;
      aarch64) arch=aarch_64; sum=8b8f18bd2b30346efbc698dd5a73dd7c805f3ef8380f6dfc95c768f3f1852f6a ;;
      *) echo "gen: unsupported architecture $(uname -m)" >&2; exit 1 ;;
    esac
    zip=/tmp/protoc.zip
    wget -q -O "$zip" "https://github.com/protocolbuffers/protobuf/releases/download/v${protoc_version}/protoc-${protoc_version}-linux-${arch}.zip"
    echo "$sum  $zip" | sha256sum -c -s
    mkdir -p /tmp/protoc && unzip -q -o "$zip" -d /tmp/protoc
    go install google.golang.org/protobuf/cmd/protoc-gen-go@v1.36.12
    go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@v1.6.2
    export PATH="/tmp/go/bin:$PATH"

    gen=/tmp/gen && mkdir -p "$gen"
    /tmp/protoc/bin/protoc -I /src/demo/proto \
      --go_out="$gen" --go_opt=paths=source_relative \
      --go-grpc_out="$gen" --go-grpc_opt=paths=source_relative \
      otellibs/demo/v1/demo.proto

    if [ "$MODE" = write ]; then
      rm -rf /out/otellibs && cp -R "$gen/otellibs" /out/
      echo "gen: wrote go/demo/gen/otellibs"
      exit 0
    fi

    if ! diff -r "$gen" /src/go/demo/gen; then
      echo "gen: go/demo/gen is stale; run go/demo/gen.sh and commit the result" >&2
      exit 1
    fi
    # Module checks; the source stays read-only, so a missing go.sum entry fails.
    cd /src/go/demo
    go mod tidy -diff
    go build -buildvcs=false -o /dev/null ./...
    go vet -buildvcs=false ./...
    echo "gen: go/demo/gen is up to date; module tidy, builds and vets"
  '
