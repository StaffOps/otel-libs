#!/usr/bin/env bash
# Runs the example apps (api, backend, process per language) in the test
# namespace, exporting OTLP/gRPC to the cluster's node-local OTel agent.
# Images come from testing/k8s/build-examples.sh.
#
# Wiring (from the examples' own code):
#   python, node, dotnet: process --HTTP--> api --gRPC--> backend (process drives traffic)
#   go:                   api --HTTP--> backend; process runs standalone root
#                         spans. The Go examples have no gRPC hop and no client
#                         in the process, so a curl container calls go-api.
#
# Layouts (LAYOUT):
#   compact (default)  one Deployment per language, the apps as containers of
#                      one pod, wired over 127.0.0.1. The test node runs at its
#                      110-pod limit, so twelve pods do not fit. go-process
#                      listens on :8080 like go-api, so it gets its own pod.
#                      Pods: go-stack, go-process, python-stack, node-stack, dotnet-stack.
#   split              one Deployment (+ Service) per app, plus a go-load Deployment.
#
# Usage: testing/k8s/examples/examples.sh [apply|delete|render]
# Env:   KUBE_CONTEXT (k8s01), NAMESPACE (dev-otel-libs),
#        REGISTRY (harbor.staffops.space/development),
#        TAG (short HEAD sha, as build-examples.sh tags; latest outside a git checkout),
#        LANGS ("go python node dotnet"), LAYOUT (compact|split)
# delete removes everything labelled app.kubernetes.io/component=example
# (honours LANGS through the otel-libs/lang label).
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
action=${1:-apply}
ctx=${KUBE_CONTEXT:-k8s01}
ns=${NAMESPACE:-dev-otel-libs}
registry=${REGISTRY:-harbor.staffops.space/development}
tag=${TAG:-$(git -C "$here" rev-parse --short HEAD 2>/dev/null || echo latest)}
langs=${LANGS:-go python node dotnet}
layout=${LAYOUT:-compact}
agent=http://otel-agent-collector.opentelemetry.svc.cluster.local:4317

# Validate before rendering anything.
for lang in $langs; do
  case "$lang" in
    go|python|node|dotnet) ;;
    *) echo "examples: unknown language '$lang' (go python node dotnet)" >&2; exit 2 ;;
  esac
done
[ -n "${langs// /}" ] || { echo "examples: LANGS is empty" >&2; exit 2; }
case "$layout" in
  compact|split) ;;
  *) echo "examples: unknown LAYOUT '$layout' (compact|split)" >&2; exit 2 ;;
esac

# probe <kind> <port> [path]  ->  readinessProbe (kind: http|grpc|tcp|none)
probe() {
  case "$1" in
    http) printf '          readinessProbe:\n            httpGet: {path: %s, port: %s}\n            periodSeconds: 10\n' "$3" "$2" ;;
    grpc) printf '          readinessProbe:\n            grpc: {port: %s}\n            periodSeconds: 10\n' "$2" ;;
    tcp)  printf '          readinessProbe:\n            tcpSocket: {port: %s}\n            periodSeconds: 10\n' "$2" ;;
    none) ;;
  esac
}

# container <lang> <app> <port|0> <probe kind> <probe path> <memory limit> [NAME=value ...]
container() {
  local lang=$1 name=$2 port=$3 pkind=$4 ppath=$5 mem=$6 kv
  shift 6
  cat <<YAML
        - name: $name
          image: $registry/otel-libs-example-$lang-$name:$tag
          imagePullPolicy: Always
YAML
  [ "$port" = 0 ] || printf '          ports:\n            - containerPort: %s\n' "$port"
  cat <<YAML
          env:
            - name: OTEL_SERVICE_NAME
              value: example-$lang-$name
            - name: OTEL_RESOURCE_ATTRIBUTES
              value: deployment.environment.name=dev
            - name: OTEL_EXPORTER_OTLP_ENDPOINT
              value: $agent
            - name: OTEL_EXPORTER_OTLP_PROTOCOL
              value: grpc
YAML
  for kv in "$@"; do
    printf '            - name: %s\n              value: "%s"\n' "${kv%%=*}" "${kv#*=}"
  done
  probe "$pkind" "$port" "$ppath"
  cat <<YAML
          resources:
            requests: {cpu: 20m, memory: 64Mi}
            limits: {cpu: 500m, memory: $mem}
$(hardening)
YAML
}

hardening() {
  cat <<YAML
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]
          volumeMounts:
            - name: tmp
              mountPath: /tmp
YAML
}

# The Go load generator: calls go-api every 15 s (health included, which must
# produce no span).
load_container() {
  local api=$1
  cat <<YAML
        - name: load
          image: curlimages/curl:8.11.1
          command: ["/bin/sh", "-c"]
          args:
            - |
              sleep 10
              while true; do
                curl -s -o /dev/null -w "detect %{http_code}\n" -X POST $api/detect -H 'Content-Type: application/json' -d '{"source":"k8s-load"}'
                curl -s -o /dev/null -w "status %{http_code}\n" $api/status/det-1
                curl -s -o /dev/null -w "summary %{http_code}\n" $api/metrics/summary
                curl -s -o /dev/null -w "health %{http_code}\n" $api/health
                sleep 15
              done
          resources:
            requests: {cpu: 5m, memory: 16Mi}
            limits: {cpu: 100m, memory: 32Mi}
$(hardening)
YAML
}

labels() { # <indent> <name> <lang>
  sed "s/^/$1/" <<YAML
app.kubernetes.io/name: $2
app.kubernetes.io/part-of: otel-libs
app.kubernetes.io/component: example
otel-libs/lang: $3
YAML
}

# deployment <name> <lang>  (containers on stdin)
deployment() {
  local name=$1 lang=$2
  cat <<YAML
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: $name
  namespace: $ns
  labels:
$(labels '    ' "$name" "$lang")
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app.kubernetes.io/name: $name
  template:
    metadata:
      labels:
$(labels '        ' "$name" "$lang")
    spec:
      automountServiceAccountToken: false
      terminationGracePeriodSeconds: 15
      securityContext:
        runAsNonRoot: true
        runAsUser: 65532
        runAsGroup: 65532
        seccompProfile:
          type: RuntimeDefault
      containers:
$(cat)
      volumes:
        - name: tmp
          emptyDir: {}
YAML
}

# service <name> <lang> <port name> <port>
service() {
  cat <<YAML
---
apiVersion: v1
kind: Service
metadata:
  name: $1
  namespace: $ns
  labels:
$(labels '    ' "$1" "$2")
spec:
  selector:
    app.kubernetes.io/name: $1
  ports:
    - name: $3
      port: $4
      targetPort: $4
YAML
}

# Per-language container sets. $1 = how the api is reached, $2 = the backend.
go_api()        { container go api 8080 http /health 128Mi "BACKEND_URL=http://$1:50051"; }
go_backend()    { container go backend 50051 http /health 128Mi; }
go_process()    { container go process 8080 http /health 128Mi; }
py_backend()    { container python backend 50051 grpc - 256Mi; }
py_api()        { container python api 8000 http /health 256Mi "BACKEND_URL=$1:50051"; }
py_process()    { container python process 8080 http /health 256Mi "API_URL=http://$1:8000"; }
node_backend()  { container node backend 50051 grpc - 256Mi PORT=50051; }
node_api()      { container node api 8000 http /health 256Mi PORT=8000 "BACKEND_URL=$1:50051"; }
node_process()  { container node process 8080 http /health 256Mi HEALTH_PORT=8080 "API_URL=http://$1:8000"; }
# No exact health path in the .NET examples (/health/ready calls the backend
# and is traced on purpose): TCP readiness. The process has no listener.
net_backend()   { container dotnet backend 5100 tcp - 384Mi; }
net_api()       { container dotnet api 8080 tcp - 384Mi "BACKEND_URL=http://$1:5100"; }
net_process()   { container dotnet process 0 none - 384Mi "API_URL=http://$1:8080"; }

render_compact() {
  local lang
  for lang in $langs; do
    case "$lang" in
      go)
        { go_backend; go_api 127.0.0.1; load_container http://127.0.0.1:8080; } | deployment go-stack go
        go_process | deployment go-process go ;;
      python) { py_backend; py_api 127.0.0.1; py_process 127.0.0.1; } | deployment python-stack python ;;
      node)   { node_backend; node_api 127.0.0.1; node_process 127.0.0.1; } | deployment node-stack node ;;
      dotnet) { net_backend; net_api 127.0.0.1; net_process 127.0.0.1; } | deployment dotnet-stack dotnet ;;
      *) echo "examples: unknown language '$lang'" >&2; exit 2 ;;
    esac
  done
}

render_split() {
  local lang
  for lang in $langs; do
    case "$lang" in
      go)
        go_backend | deployment go-backend go; service go-backend go http 50051
        go_api go-backend | deployment go-api go; service go-api go http 8080
        go_process | deployment go-process go
        load_container http://go-api:8080 | deployment go-load go ;;
      python)
        py_backend | deployment python-backend python; service python-backend python grpc 50051
        py_api python-backend | deployment python-api python; service python-api python http 8000
        py_process python-api | deployment python-process python ;;
      node)
        node_backend | deployment node-backend node; service node-backend node grpc 50051
        node_api node-backend | deployment node-api node; service node-api node http 8000
        node_process node-api | deployment node-process node ;;
      dotnet)
        net_backend | deployment dotnet-backend dotnet; service dotnet-backend dotnet grpc 5100
        net_api dotnet-backend | deployment dotnet-api dotnet; service dotnet-api dotnet http 8080
        net_process dotnet-api | deployment dotnet-process dotnet ;;
      *) echo "examples: unknown language '$lang'" >&2; exit 2 ;;
    esac
  done
}

render() {
  case "$layout" in
    compact) render_compact ;;
    split) render_split ;;
    *) echo "examples: unknown LAYOUT '$layout'" >&2; exit 2 ;;
  esac
}

case "$action" in
  render) render ;;
  apply)
    # Render fully first: a render error must not leave a half-applied stack.
    manifests=$(render)
    kubectl --context "$ctx" apply -f - <<<"$manifests"
    echo "tag=$tag"
    ;;
  delete)
    read -ra lang_list <<<"$langs"
    sel="app.kubernetes.io/component=example,otel-libs/lang in ($(IFS=,; echo "${lang_list[*]}"))"
    kubectl --context "$ctx" -n "$ns" delete deployments,services,jobs -l "$sel"
    ;;
  *) echo "usage: $0 [apply|delete|render]" >&2; exit 2 ;;
esac
