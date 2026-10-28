#!/usr/bin/env bash
# End-to-end smoke against a real otel/opentelemetry-collector-contrib.
#
# Per language (Go smoke program, Python python-process example) and per
# combination in combos.sh — gRPC; http/protobuf with an /otlp path prefix; a
# per-signal traces endpoint; TLS with a generated CA — each signal must
# arrive at the expected receiver, in a batch whose resource carries the
# combination's service.name. Combinations broken by known bugs are expected
# failures (XFAIL <task>); an expected failure that passes is an error (XPASS).
#
#   testing/e2e/smoke.sh
#
# The Collector is stopped at exit only when this run started it. A Collector
# that was already running is reused as is (not recreated, not stopped) and
# only its log lines since this run began are read.
set -euo pipefail

cd "$(dirname "$0")/.."
compose=(docker compose -f docker-compose.yml --profile e2e)

collector_running() {
  "${compose[@]}" ps --status running --services 2>/dev/null | grep -qx collector
}
started_collector=0
stop_collector() {
  if [ "$started_collector" = 1 ]; then
    "${compose[@]}" stop collector >/dev/null 2>&1 || true
    started_collector=0
  fi
}
trap stop_collector EXIT
trap 'exit 130' INT TERM

since=$(( $(date +%s) - 2 )) # small margin for host/VM clock skew
if collector_running; then
  echo "smoke: reusing the Collector that was already running (it will be left running)"
  logs_since=(--since "$since")
else
  started_collector=1
  "${compose[@]}" up -d --force-recreate --quiet-pull collector
  logs_since=()
fi
"${compose[@]}" run --rm --quiet-pull go-smoke
"${compose[@]}" run --rm --quiet-pull python-smoke
sleep 3 # let the debug exporter print the last batches
logs=$("${compose[@]}" logs --no-color --no-log-prefix ${logs_since[@]+"${logs_since[@]}"} collector)
stop_collector

# One "<signal>|<service.name>|<receiver>" line per recorded batch that holds a smoke marker.
seen=$(awk '
  # The debug exporter prints "ResourceSpans #N", "ResourceMetrics #N", "ResourceLog #N".
  /ResourceSpans #/   { k = "Spans";   svc = ""; rcv = ""; next }
  /ResourceMetrics #/ { k = "Metrics"; svc = ""; rcv = ""; next }
  /ResourceLogs? #/   { k = "Logs";    svc = ""; rcv = ""; next }
  /-> service\.name: Str\(/ { s = $0; sub(/.*service\.name: Str\(/, "", s); sub(/\).*/, "", s); svc = s }
  /-> e2e\.receiver: Str\(/ { s = $0; sub(/.*e2e\.receiver: Str\(/, "", s); sub(/\).*/, "", s); rcv = s }
  k == "Spans"   && /Name +: (e2e-smoke-span|process\.queue-consume)$/       { print "traces|" svc "|" rcv }
  k == "Metrics" && /-> Name: (e2e\.smoke\.runs|notifications\.queue_depth)$/ { print "metrics|" svc "|" rcv }
  k == "Logs"    && /Body: Str\((e2e-smoke-log|Starting notification workers)\)/        { print "logs|" svc "|" rcv }
' <<<"$logs" | sort -u)

# lang combo signal receiver expectation
#   ok / xfail:<task>               the batch must arrive at that receiver
#   absent / absent-xfail:<task>    it must NOT arrive there
expect='go     grpc        traces  default   ok
go     grpc        metrics default   ok
go     grpc        logs    default   ok
go     http-prefix traces  prefix    ok
go     http-prefix metrics prefix    ok
go     http-prefix logs    prefix    ok
go     per-signal  traces  persignal ok
go     per-signal  traces  default   absent
go     per-signal  metrics default   ok
go     per-signal  logs    default   ok
go     tls         traces  tls       ok
go     tls         metrics tls       ok
go     tls         logs    tls       ok
python grpc        traces  default   ok
python grpc        metrics default   ok
python grpc        logs    default   ok
python http-prefix traces  prefix    ok
python http-prefix metrics prefix    ok
python http-prefix logs    prefix    ok
python per-signal  traces  persignal ok
python per-signal  traces  default   absent
python per-signal  metrics default   ok
python per-signal  logs    default   ok
python tls         traces  tls       ok
python tls         metrics tls       ok
python tls         logs    tls       ok'

status=0
while read -r lang combo signal receiver want; do
  key="$signal|e2e-$lang-$combo|$receiver"
  if grep -qxF "$key" <<<"$seen"; then got=ok; else got=miss; fi
  case "$want/$got" in
    ok/ok | absent/miss)       verdict="OK   " ;;
    ok/miss)                   verdict="MISS "; status=1 ;;
    absent/ok)                 verdict="LEAK "; status=1 ;;
    xfail*/miss | absent-xfail*/ok) verdict="XFAIL" ;;
    xfail*/ok | absent-xfail*/miss) verdict="XPASS"; status=1 ;;
  esac
  case "$want" in absent*) arrow="-/->" ;; *) arrow="->  " ;; esac
  printf '%s %-6s %-11s %-7s %s %-9s %s\n' "$verdict" "$lang" "$combo" "$signal" "$arrow" "$receiver" "${want#ok}"
done <<<"$expect"
exit $status
