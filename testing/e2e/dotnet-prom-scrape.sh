#!/usr/bin/env bash
# .NET Prometheus fallback scraped from ANOTHER container (design D-NET11, task C3).
#
# The 1.20.x Prometheus HttpListener binds `localhost` by default, which no
# other container or pod can reach; the helper replaces the prefix with
# `http://+:<port>/metrics/`. This check builds the dotnet-process example (a
# worker service, no ASP.NET Core pipeline) against the helper package packed
# from this tree (task C17) with no OTLP endpoint,
# then scrapes it by container name from a separate curl container on a
# private network, once in the Prometheus text format and once asking for
# OpenMetrics 1.0.0 (what a Prometheus server negotiates first). A
# loopback-only bind makes the scrape fail. The listener binds IPv4 only (as
# 0.2.x did); this check does not cover IPv6. Then a second app container with
# OTEL_EXPORTER_PROMETHEUS_HOST=127.0.0.1, then =localhost (task C12), must answer
# on loopback (127.0.0.1 and localhost) inside itself and refuse the other
# container (the bind-host negative control).
#
#   testing/e2e/dotnet-prom-scrape.sh
#   DOTNET_DIR=/path/to/dotnet testing/e2e/dotnet-prom-scrape.sh   # another tree (e.g. to see it fail)
#
# Creates one network and two app containers in turn (unique names), removes exactly
# those at exit; never prunes, never removes images or volumes.
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
DOTNET_DIR="${DOTNET_DIR:-$root/dotnet}"
# Runtime metrics come from OpenTelemetry.Instrumentation.Runtime on the .NET 8
# runtime and from the built-in System.Runtime meter on .NET 9+; either passes.
SDK_IMAGE="${SDK_IMAGE:-mcr.microsoft.com/dotnet/sdk:8.0}"
CURL_IMAGE="curlimages/curl:8.11.1"
id="otel-libs-dotnet-scrape-$$"
id2="$id-loopback"
port=9464
# The app container's command: a container-local copy without the host's bin/obj
# (the repo is mounted read-only), then StaffOps.OtelHelper packed into the local
# feed /packages and the example restored against that .nupkg
# (example/nuget.packages.config), as its Dockerfile does, never the project
# reference (task C17).
app_cmd='mkdir /w && cd /src && tar --exclude=bin --exclude=obj -cf - . | tar -xf - -C /w && cd /w &&
  dotnet pack StaffOps.OtelHelper/StaffOps.OtelHelper.csproj -c Release -o /packages --nologo -v quiet &&
  ver=$(ls /packages/StaffOps.OtelHelper.[0-9]*.nupkg | sed "s#.*/StaffOps[.]OtelHelper[.]\(.*\)[.]nupkg#\1#") &&
  dotnet restore example/dotnet-process --configfile example/nuget.packages.config -p:OtelHelperVersion="$ver" -v quiet &&
  dotnet run --project example/dotnet-process -c Release --no-restore --property:OtelHelperVersion="$ver"'

cleanup() {
  docker rm -f "$id" "$id2" >/dev/null 2>&1 || true
  docker network rm "$id" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 130' INT TERM

docker network create "$id" >/dev/null
docker run -d --name "$id" --network "$id" -v "$DOTNET_DIR:/src:ro" \
  -e SERVICE_NAME=e2e-dotnet-scrape -e ENVIRONMENT=DEV -e OTEL_HELPER_METRICS_PORT="$port" \
  "$SDK_IMAGE" sh -c "$app_cmd" >/dev/null

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

# Negative control (task C12, contract A29; fix round 1 adds localhost): with
# OTEL_EXPORTER_PROMETHEUS_HOST=127.0.0.1, then =localhost, the listener answers
# inside its container — on 127.0.0.1 and, through curl, on localhost — and
# refuses a connect from another container. One app container at a time.
docker rm -f "$id" >/dev/null
for bind in 127.0.0.1 localhost; do
  docker rm -f "$id2" >/dev/null 2>&1 || true
  docker run -d --name "$id2" --network "$id" -v "$DOTNET_DIR:/src:ro" \
    -e SERVICE_NAME=e2e-dotnet-scrape -e ENVIRONMENT=DEV -e OTEL_HELPER_METRICS_PORT="$port" \
    -e OTEL_EXPORTER_PROMETHEUS_HOST="$bind" \
    "$SDK_IMAGE" sh -c "$app_cmd" >/dev/null
  loopback=""
  for _ in $(seq 1 100); do
    loopback=$(docker exec "$id2" bash -c "exec 3<>/dev/tcp/127.0.0.1/$port && printf 'GET /metrics HTTP/1.1\r\nHost: 127.0.0.1:$port\r\nConnection: close\r\n\r\n' >&3 && head -1 <&3" 2>/dev/null || true)
    case "$loopback" in *" 200 "*) break ;; esac
    sleep 3
  done
  case "$loopback" in
    *" 200 "*) ;;
    *) echo "dotnet-prom-scrape: FAIL — HOST=$bind: the listener never answered on 127.0.0.1 inside its container" >&2
       docker logs --tail 40 "$id2" >&2 || true
       exit 1 ;;
  esac
  # localhost: the .NET listener's localhost prefix binds the resolver's first address (::1 when
  # the container has an IPv6 loopback). Go (Prometheus, vmagent) dials ::1 first (measured);
  # curl resolves with AI_ADDRCONFIG and, without a non-loopback IPv6 address, connects to
  # 127.0.0.1, which routes Host: localhost to 404 (documented residual). One of the two must answer.
  code_any=$(docker exec "$id2" curl -s -o /dev/null -m 5 -w '%{http_code}' "http://localhost:$port/metrics" || true)
  code_v6=$(docker exec "$id2" curl -s -6 -o /dev/null -m 5 -w '%{http_code}' "http://localhost:$port/metrics" || true)
  if [ "$code_any" != 200 ] && [ "$code_v6" != 200 ]; then
    echo "dotnet-prom-scrape: FAIL — HOST=$bind: http://localhost:$port/metrics answered $code_any (curl) and $code_v6 (curl -6), want 200 on one" >&2
    exit 1
  fi
  rc=0
  docker run --rm --network "$id" "$CURL_IMAGE" -sS -o /dev/null -m 5 "http://$id2:$port/metrics" 2>/dev/null || rc=$?
  if [ "$rc" != 7 ]; then
    echo "dotnet-prom-scrape: FAIL — with OTEL_EXPORTER_PROMETHEUS_HOST=$bind another container got curl exit $rc (want 7, connection refused)" >&2
    exit 1
  fi
  echo "dotnet-prom-scrape: PASS — with OTEL_EXPORTER_PROMETHEUS_HOST=$bind the listener answers on 127.0.0.1 (200) and localhost (curl $code_any, curl -6 $code_v6) inside its container and refuses another container (curl exit 7)"
done
