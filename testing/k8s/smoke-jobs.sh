#!/usr/bin/env bash
# Runs the e2e smoke images as Kubernetes Jobs in the test namespace and
# exports to the cluster's node-local OTel agent (grpc and http/protobuf).
# Usage: testing/k8s/smoke-jobs.sh [apply [--verify]|delete|render]
# Env:   KUBE_CONTEXT (k8s01), NAMESPACE (dev-otel-libs),
#        REGISTRY (harbor.staffops.space/development),
#        TAG (short HEAD sha, as build-images.sh tags; latest outside a git checkout),
#        RUN (a run id, default: UTC timestamp), LANGS ("go python node dotnet"),
#        VERIFY (1 = same as --verify), WAIT_TIMEOUT (seconds, 300),
#        VERIFY_DELAY (seconds between the Jobs finishing and the checks, 30)
# apply --verify polls the run's Jobs until all succeeded, one failed, or
# WAIT_TIMEOUT, then runs testing/k8s/verify.py smoke --run $RUN (read-only
# backend queries); the exit code is non-zero when a Job did not succeed or a
# check failed.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"

action=${1:-apply}
ctx=${KUBE_CONTEXT:-k8s01}
ns=${NAMESPACE:-dev-otel-libs}
registry=${REGISTRY:-harbor.staffops.space/development}
tag=${TAG:-$(git -C "$here" rev-parse --short HEAD 2>/dev/null || echo latest)}
run=${RUN:-$(date -u +%Y%m%d%H%M%S)}
langs=${LANGS:-go python node dotnet}
agent=otel-agent-collector.opentelemetry.svc.cluster.local

# Validate before rendering anything (a Job name must be a DNS label).
for lang in $langs; do
  case "$lang" in
    go|python|node|dotnet) ;;
    *) echo "smoke-jobs: unknown language '$lang' (go python node dotnet)" >&2; exit 2 ;;
  esac
done
[ -n "${langs// /}" ] || { echo "smoke-jobs: LANGS is empty" >&2; exit 2; }
[[ "$run" =~ ^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$ ]] ||
  { echo "smoke-jobs: RUN '$run' must be lowercase letters, digits and '-' (max 32)" >&2; exit 2; }

# wait_jobs: poll the run's Jobs; 0 when every one succeeded, 1 when one failed
# or WAIT_TIMEOUT passed.
wait_jobs() {
  local want=$(( $(wc -w <<<"$langs") * 2 )) deadline=$(( SECONDS + ${WAIT_TIMEOUT:-300} ))
  local out ok failed
  while :; do
    out=$(kubectl --context "$ctx" -n "$ns" get jobs -l "otel-libs/run=$run" \
      -o jsonpath='{range .items[*]}{.metadata.name},{.status.succeeded},{.status.failed}{"\n"}{end}')
    ok=$(awk -F, '$2 + 0 >= 1' <<<"$out" | wc -l | tr -d ' ')
    failed=$(awk -F, '$3 + 0 >= 1 && $2 + 0 < 1 {print $1}' <<<"$out")
    if [ -n "$failed" ]; then
      echo "smoke-jobs: failed: $(tr '\n' ' ' <<<"$failed")" >&2; return 1
    fi
    [ "$ok" -ge "$want" ] && { echo "smoke-jobs: $ok/$want Jobs succeeded"; return 0; }
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "smoke-jobs: timeout, $ok/$want Jobs succeeded" >&2; return 1
    fi
    sleep 5
  done
}

render() {
  for lang in $langs; do
    for combo in grpc http; do
      if [ "$combo" = grpc ]; then
        endpoint="http://$agent:4317"; protocol=grpc
      else
        endpoint="http://$agent:4318"; protocol=http/protobuf
      fi
      cat <<YAML
---
apiVersion: batch/v1
kind: Job
metadata:
  name: smoke-$lang-$combo-$run
  namespace: $ns
  labels:
    app.kubernetes.io/part-of: otel-libs
    app.kubernetes.io/component: e2e-smoke
    otel-libs/lang: $lang
    otel-libs/combo: $combo
    otel-libs/run: "$run"
spec:
  backoffLimit: 0
  activeDeadlineSeconds: 180
  ttlSecondsAfterFinished: 86400
  template:
    metadata:
      labels:
        app.kubernetes.io/part-of: otel-libs
        app.kubernetes.io/component: e2e-smoke
        otel-libs/lang: $lang
        otel-libs/combo: $combo
        otel-libs/run: "$run"
    spec:
      restartPolicy: Never
      automountServiceAccountToken: false
      securityContext:
        runAsNonRoot: true
        runAsUser: 65532
        runAsGroup: 65532
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: smoke
          image: $registry/otel-libs-smoke-$lang:$tag
          imagePullPolicy: Always
          env:
            - name: OTEL_SERVICE_NAME
              value: e2e-$lang-k8s-$combo
            - name: OTEL_RESOURCE_ATTRIBUTES
              value: deployment.environment.name=dev,otel_libs.run=$run
            - name: OTEL_EXPORTER_OTLP_ENDPOINT
              value: $endpoint
            - name: OTEL_EXPORTER_OTLP_PROTOCOL
              value: $protocol
            - name: OTEL_METRIC_EXPORT_INTERVAL
              value: "1000"
          resources:
            requests: {cpu: 50m, memory: 64Mi}
            limits: {cpu: 500m, memory: 256Mi}
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]
          volumeMounts:
            - name: tmp
              mountPath: /tmp
      volumes:
        - name: tmp
          emptyDir: {}
YAML
    done
  done
}

case "$action" in
  render) render ;;
  apply)
    verify=${VERIFY:-0}
    case "${2:-}" in
      --verify) verify=1 ;;
      "") ;;
      *) echo "usage: $0 apply [--verify]" >&2; exit 2 ;;
    esac
    manifests=$(render)
    kubectl --context "$ctx" apply -f - <<<"$manifests"
    echo "run=$run tag=$tag"
    [ "$verify" = 1 ] || exit 0
    rc=0
    wait_jobs || rc=1
    sleep "${VERIFY_DELAY:-30}"
    python3 -I "$here/verify.py" --context "$ctx" --namespace "$ns" --langs "$langs" \
      --since 1h smoke --run "$run" || rc=1
    exit "$rc"
    ;;
  delete)
    kubectl --context "$ctx" -n "$ns" delete jobs -l app.kubernetes.io/component=e2e-smoke
    ;;
  *) echo "usage: $0 [apply [--verify]|delete|render]" >&2; exit 2 ;;
esac
