#!/usr/bin/env bash
# Checks that the Python smoke program exits 1 when the OTLP endpoint refuses
# the export (a closed port), for grpc and http/protobuf. Local Docker only; no
# cluster, no registry push. python-smoke.py detects the refusal from the OTLP
# exporters' ERROR log records (the SDK's force_flush/shutdown still succeed).
#
# Usage: testing/k8s/images/check-python-refused.sh
#   IMAGE  image to run (default: build otel-libs-smoke-python:check locally
#          from the working tree with images/python.Dockerfile)
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
root="$(git -C "$here" rev-parse --show-toplevel)"
image=${IMAGE:-}
if [ -z "$image" ]; then
  image=otel-libs-smoke-python:check
  docker build -q -f "$here/python.Dockerfile" -t "$image" "$root" >/dev/null
fi

rc=0
for protocol in grpc http/protobuf; do
  status=0
  docker run --rm \
    -e OTEL_SERVICE_NAME=e2e-python-refused \
    -e OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=dev \
    -e OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:9 \
    -e OTEL_EXPORTER_OTLP_PROTOCOL="$protocol" \
    -e OTEL_EXPORTER_OTLP_TIMEOUT=2000 \
    "$image" >/dev/null 2>&1 || status=$?
  if [ "$status" = 1 ]; then
    echo "PASS  $protocol: refused export -> exit 1"
  else
    echo "FAIL  $protocol: refused export -> exit $status (want 1)"
    rc=1
  fi
done
exit "$rc"
