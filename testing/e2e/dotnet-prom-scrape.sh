#!/usr/bin/env bash
# .NET Prometheus fallback scraped from ANOTHER container (design D-NET11, task C3).
#
# The 1.20.x Prometheus HttpListener binds `localhost` by default, which no
# other container or pod can reach; the helper replaces the prefix with
# `http://+:<port>/metrics/`. This check builds the dotnet-process example (a
# worker service, no ASP.NET Core pipeline) from source with no OTLP endpoint,
# then scrapes it by container name from a separate curl container on a
# private network, once in the Prometheus text format and once asking for
# OpenMetrics 1.0.0 (what a Prometheus server negotiates first). A
# loopback-only bind makes the scrape fail. The listener binds IPv4 only (as
# 0.2.x did); this check does not cover IPv6.
#
#   testing/e2e/dotnet-prom-scrape.sh
#   DOTNET_DIR=/path/to/dotnet testing/e2e/dotnet-prom-scrape.sh   # another tree (e.g. to see it fail)
#
# Creates one network and one app container (unique names), removes exactly
# those at exit; never prunes, never removes images or volumes.
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
DOTNET_DIR="${DOTNET_DIR:-$root/dotnet}"
# Runtime metrics come from OpenTelemetry.Instrumentation.Runtime on the .NET 8
# runtime and from the built-in System.Runtime meter on .NET 9+; either passes.
SDK_IMAGE="${SDK_IMAGE:-mcr.microsoft.com/dotnet/sdk:8.0}"
CURL_IMAGE="curlimages/curl:8.11.1"
id="otel-libs-dotnet-scrape-$$"
port=9464

cleanup() {
  docker rm -f "$id" >/dev/null 2>&1 || true
  docker network rm "$id" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 130' INT TERM

docker network create "$id" >/dev/null
# Container-local copy without the host's bin/obj: the repo is mounted read-only.
docker run -d --name "$id" --network "$id" -v "$DOTNET_DIR:/src:ro" \
  -e SERVICE_NAME=e2e-dotnet-scrape -e ENVIRONMENT=DEV -e OTEL_HELPER_METRICS_PORT="$port" \
  "$SDK_IMAGE" sh -c 'mkdir /w && cd /src && tar --exclude=bin --exclude=obj -cf - . | tar -xf - -C /w &&
    cd /w && dotnet run --project example/dotnet-process -c Release' >/dev/null

# The first attempts are refused while the example builds (up to ~5 min).
body=$(docker run --rm --network "$id" "$CURL_IMAGE" -sS -f -m 5 \
  --retry 100 --retry-delay 3 --retry-connrefused --retry-all-errors \
  "http://$id:$port/metrics") || {
  echo "dotnet-prom-scrape: FAIL — http://$id:$port/metrics not reachable from another container" >&2
  docker logs --tail 40 "$id" >&2 || true
  exit 1
}

fail=0
check() { # <format> <body> <needle>...
  local format="$1" text="$2"; shift 2
  for want in "$@"; do
    if ! grep -qF -- "$want" <<<"$text"; then
      echo "dotnet-prom-scrape: FAIL — $format scrape lacks $want" >&2
      fail=1
    fi
  done
}
check text "$body" 'target_info{' 'service_name="e2e-dotnet-scrape"'
if ! grep -qE 'otel_scope_name="(OpenTelemetry\.Instrumentation\.Runtime|System\.Runtime)"' <<<"$body"; then
  echo "dotnet-prom-scrape: FAIL — text scrape has no runtime metrics" >&2
  fail=1
fi

# OpenMetrics: the response must be OpenMetrics 1.0.0 and carry the "# EOF" line.
om=$(docker run --rm --network "$id" "$CURL_IMAGE" -sS -f -m 10 -D - \
  -H 'Accept: application/openmetrics-text;version=1.0.0' "http://$id:$port/metrics") || {
  echo "dotnet-prom-scrape: FAIL — OpenMetrics scrape failed" >&2
  exit 1
}
check OpenMetrics "$om" 'application/openmetrics-text; version=1.0.0' 'target_info{' '# EOF'
[ "$fail" = 0 ] || exit 1
echo "dotnet-prom-scrape: PASS — scraped http://$id:$port/metrics from another container, text ($(grep -c '^# TYPE' <<<"$body") metric families) and OpenMetrics"
