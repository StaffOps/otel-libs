#!/usr/bin/env bash
# End-to-end smoke against a real otel/opentelemetry-collector-contrib.
#
# Per language (Go, Node and .NET smoke programs, Python python-process example) and per
# combination in combos.sh — gRPC; http/protobuf with an /otlp path prefix; a
# per-signal traces endpoint; TLS with a generated CA — each signal must
# arrive at the expected receiver, in a batch whose resource carries the
# combination's service.name. Combinations broken by known bugs are expected
# failures (XFAIL <task>); an expected failure that passes is an error (XPASS).
#
#   testing/e2e/smoke.sh
#   SMOKE_LANGS=node NODE_VERSION=22.15.0 testing/e2e/smoke.sh   # one language, another Node line
#   SMOKE_LANGS=dotnet testing/e2e/smoke.sh                       # .NET only (packs the helper, task C17)
#
# SMOKE_LANGS (default "go python node dotnet") picks the languages run and checked;
# NODE_VERSION picks the node-smoke image line (docker-compose.yml, default 24).
# With dotnet selected, the .NET Prometheus fallback is also scraped from another
# container (e2e/dotnet-prom-scrape.sh, its own network and containers) and
# reported as one more row; SMOKE_DOTNET_SCRAPE=0 skips that row.
#
# The Collector is stopped at exit only when this run started it. A Collector
# that was already running is reused as is (not recreated, not stopped) and
# only its log lines since this run began are read.
set -euo pipefail

cd "$(dirname "$0")/.."
. e2e/combos.sh
compose=(docker compose -f docker-compose.yml --profile e2e)
# Normalized to " a b c " (tabs, newlines and commas count as separators).
langs=" $(printf '%s' "${SMOKE_LANGS:-go python node dotnet}" | tr -s ' \t\n,' '    ' | sed 's/^ *//; s/ *$//') "
if [ "$langs" = "  " ]; then
  echo "smoke: SMOKE_LANGS names no language (go, python, node, dotnet)" >&2
  exit 2
fi
for lang in $langs; do
  case "$lang" in
    go | python | node | dotnet) ;;
    *) echo "smoke: unknown language in SMOKE_LANGS: $lang (go, python, node, dotnet)" >&2; exit 2 ;;
  esac
done

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
for lang in go python node dotnet; do
  case "$langs" in *" $lang "*) "${compose[@]}" run --rm --quiet-pull "$lang-smoke" ;; esac
done
sleep 3 # let the debug exporter print the last batches
logs=$("${compose[@]}" logs --no-color --no-log-prefix ${logs_since[@]+"${logs_since[@]}"} collector)
stop_collector

# One "<signal>|<service.name>|<receiver>" line per recorded batch that holds a smoke marker,
# plus "logs-correlated|<service.name>|-" when every marker log record of that
# service carries the Trace ID of one of its marker spans (2026-11-03: the logs
# are exported in the span's context; Python: "Failed to dispatch", written
# inside process.queue-consume, since its startup log has no span).
seen=$(awk '
  # The debug exporter prints "ResourceSpans #N", "ResourceMetrics #N", "ResourceLog #N".
  /ResourceSpans #/   { k = "Spans";   svc = ""; rcv = ""; next }
  /ResourceMetrics #/ { k = "Metrics"; svc = ""; rcv = ""; next }
  /ResourceLogs? #/   { k = "Logs";    svc = ""; rcv = ""; next }
  /-> service\.name: Str\(/ { s = $0; sub(/.*service\.name: Str\(/, "", s); sub(/\).*/, "", s); svc = s }
  /-> e2e\.receiver: Str\(/ { s = $0; sub(/.*e2e\.receiver: Str\(/, "", s); sub(/\).*/, "", s); rcv = s }
  /Span #[0-9]/ { tid = "" }
  k == "Spans" && /Trace ID +: / { tid = $0; sub(/.*Trace ID +: */, "", tid) }
  k == "Spans"   && /Name +: (e2e-smoke-span|process\.queue-consume)$/       { print "traces|" svc "|" rcv; if (tid != "") span[svc, tid] = 1 }
  k == "Metrics" && /-> Name: (e2e\.smoke\.runs|notifications\.queue_depth)$/ { print "metrics|" svc "|" rcv }
  k == "Logs"    && /Body: Str\((e2e-smoke-log|Starting notification workers)\)/        { print "logs|" svc "|" rcv }
  /LogRecord #[0-9]/ { corr = 0 }
  k == "Logs" && /Body: Str\((e2e-smoke-log|Failed to dispatch)\)/ { corr = 1 }
  k == "Logs" && corr && /Trace ID: / { t = $0; sub(/.*Trace ID: */, "", t); n[svc]++; lt[svc, n[svc]] = t; corr = 0 }
  END {
    for (s in n) {
      ok = 1
      for (i = 1; i <= n[s]; i++) if (lt[s, i] == "" || !((s, lt[s, i]) in span)) ok = 0
      if (ok) print "logs-correlated|" s "|-"
    }
  }
' <<<"$logs" | sort -u)

# lang combo signal receiver expectation
#   ok / xfail:<task>               the batch must arrive at that receiver
#   absent / absent-xfail:<task>    it must NOT arrive there
# combo "all": the check must hold for every combination in combos.sh.
expect='go     grpc        traces  default   ok
go     grpc        metrics default   ok
go     grpc        logs    default   ok
go     http-prefix traces  prefix    ok
go     http-prefix metrics prefix    ok
go     http-prefix logs    prefix    ok
go     per-signal  traces  persignal ok
go     per-signal  traces  default   absent
go     per-signal  metrics persignal absent
go     per-signal  logs    persignal absent
go     per-signal  metrics default   ok
go     per-signal  logs    default   ok
go     tls         traces  tls       ok
go     tls         metrics tls       ok
go     tls         logs    tls       ok
go     all         logs-correlated -     ok
python grpc        traces  default   ok
python grpc        metrics default   ok
python grpc        logs    default   ok
python http-prefix traces  prefix    ok
python http-prefix metrics prefix    ok
python http-prefix logs    prefix    ok
python per-signal  traces  persignal ok
python per-signal  traces  default   absent
python per-signal  metrics persignal absent
python per-signal  logs    persignal absent
python per-signal  metrics default   ok
python per-signal  logs    default   ok
python tls         traces  tls       ok
python tls         metrics tls       ok
python tls         logs    tls       ok
python all         logs-correlated -     ok
node   grpc        traces  default   ok
node   grpc        metrics default   ok
node   grpc        logs    default   ok
node   http-prefix traces  prefix    ok
node   http-prefix metrics prefix    ok
node   http-prefix logs    prefix    ok
node   per-signal  traces  persignal ok
node   per-signal  traces  default   absent
node   per-signal  metrics persignal absent
node   per-signal  logs    persignal absent
node   per-signal  metrics default   ok
node   per-signal  logs    default   ok
node   tls         traces  tls       ok
node   tls         metrics tls       ok
node   tls         logs    tls       ok
node   all         logs-correlated -     ok
dotnet grpc        traces  default   ok
dotnet grpc        metrics default   ok
dotnet grpc        logs    default   ok
dotnet http-prefix traces  prefix    ok
dotnet http-prefix metrics prefix    ok
dotnet http-prefix logs    prefix    ok
dotnet per-signal  traces  persignal ok
dotnet per-signal  traces  default   absent
dotnet per-signal  metrics persignal absent
dotnet per-signal  logs    persignal absent
dotnet per-signal  metrics default   ok
dotnet per-signal  logs    default   ok
dotnet tls         traces  tls       ok
dotnet tls         metrics tls       ok
dotnet tls         logs    tls       ok
dotnet all         logs-correlated -     ok'

status=0
while read -r lang combo signal receiver want; do
  case "$langs" in *" $lang "*) ;; *) continue ;; esac
  if [ "$combo" = all ]; then
    got=ok
    while IFS='|' read -r name _; do
      grep -qxF "$signal|e2e-$lang-$name|$receiver" <<<"$seen" || got=miss
    done <<<"$COMBOS"
  else
    if grep -qxF "$signal|e2e-$lang-$combo|$receiver" <<<"$seen"; then got=ok; else got=miss; fi
  fi
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

# .NET Prometheus fallback scraped from another container (design D-NET11, task C17).
case "$langs" in
  *" dotnet "*)
    if [ "${SMOKE_DOTNET_SCRAPE:-1}" = 0 ]; then
      echo "SKIP  dotnet prometheus scrape from another container (SMOKE_DOTNET_SCRAPE=0)"
    elif scrape=$(e2e/dotnet-prom-scrape.sh 2>&1); then
      echo "OK    dotnet prometheus scrape from another container (e2e/dotnet-prom-scrape.sh)"
      grep 'PASS' <<<"$scrape" | sed 's/^/      /'
    else
      printf '%s\n' "$scrape" | tail -20 >&2
      echo "MISS  dotnet prometheus scrape from another container (e2e/dotnet-prom-scrape.sh)"
      status=1
    fi
    ;;
esac
exit $status
