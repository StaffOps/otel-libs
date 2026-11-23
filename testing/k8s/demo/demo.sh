#!/usr/bin/env bash
# Demo environment on the test cluster (specs/demo-environment). Task DE2:
# the shared backing services — one PostgreSQL and one Redis for every
# language (DE-D4), credentials generated once and never printed (DE-D13),
# requests from the Budget table (DE-D12). Task DE4: the demo apps — per
# language a worker, an api and a backend Deployment (+ Service for api and
# backend), one container per pod (DE-D2 split layout).
#
#   demo-postgres  postgres:17.11-alpine by digest, data on emptyDir; one
#                  database and one role demo_<lang> per language, each role
#                  allowed to connect to its own database only. Roles are
#                  created from SCRAM-SHA-256 verifiers (scram.py, password on
#                  stdin) by an initdb script held in a Secret, so the server
#                  never sees a role's plaintext; the script runs with
#                  log_min_error_statement=panic.
#   demo-redis     redis:7.4.12-alpine by digest; redis.conf (requirepass)
#                  from a Secret; probes read the password from the mounted
#                  file into REDISCLI_AUTH, never `-a`. No persistence. No
#                  password is a container env value (PostgreSQL reads
#                  POSTGRES_PASSWORD_FILE).
#   demo-<lang>-worker|api|backend
#                  images from testing/k8s/build-demo.sh; OTLP/gRPC to the
#                  node-local agent, service name demo-<lang>-<app>, resource
#                  deployment.environment.name=dev,otel_libs.run=<run>, 10 s
#                  metric interval (DE-D2). The worker and the api mount the
#                  `password` key of demo-redis-auth, the backend its key
#                  demo_<lang> of demo-postgres-auth, as files (mode 0440,
#                  group = fsGroup) read through DEMO_*_PASSWORD_FILE: no
#                  password is a container env value (DE-D13 r3.3). Probes:
#                  api GET /health, backend grpc.health.v1 (startup and
#                  readiness; liveness is a TCP check, so a backing-service
#                  blip makes a pod unready, not restarted).
#
# Usage: testing/k8s/demo/demo.sh apply [--lang <lang> | --all]
#                                 | scale --lang <lang> <n> | scale --all <n>
#                                 | delete --lang <lang> | delete --all | render
#   apply   (no option) the backing services only. K-D2 pre-flight, then
#           creates the Secrets (stringData through `kubectl create -f -`;
#           never client-side apply, which stores the plaintext in the
#           last-applied annotation, never --from-literal) and server-side
#           applies the Deployments and Services; after the rollout it checks
#           the init (no PostgreSQL restart, the four roles exist). If the
#           backing services already run, it creates nothing, re-checks the
#           init and prints their run id: passwords live for the backing
#           services' lifetime (apply -> delete --all), and language rotation
#           windows reuse them (DE-D13). An explicit RUN that differs from the
#           running services' run is an error.
#   apply --lang <lang> | --all   the backing services as above (reused when
#           running), then the apps of one language or of every language in
#           LANGS, with the backing services' run id. K-D2 pre-flight on the
#           apps' requests (already running app pods of the same languages are
#           counted again: conservative); when --all does not fit, nothing is
#           applied and it says to rotate one language at a time (DE-O8).
#           During the rollout and DEMO_WATCH_SECONDS after it, the K-D2
#           FailedScheduling watch (schedwatch.py) runs; on a hit the apps of
#           this apply are scaled to 0 and apply fails; when every look-up
#           was blind (schedwatch exit 2) apply fails too, with the apps left
#           up, and a partly blind watch is a warning with the counts. The
#           apps are applied with --force-conflicts: after `scale`, the
#           `kubectl` field manager owns .spec.replicas, and a plain
#           server-side apply of replicas 1 would fail with a conflict.
#   scale --lang <lang> <n> | --all <n>  sets the replicas of the demo app
#           Deployments of one language (or of every language in LANGS) to n
#           (0-999); the backing services are never scaled. For n > 0, the
#           K-D2 pre-flight on n times the apps' requests (running app pods
#           counted again: conservative), then the rollout and the
#           FailedScheduling watch as for apply; for n = 0 no pre-flight, and
#           it waits for the pods to go. Refuses when the apps are not applied.
#   delete --lang <lang>  deletes that language's demo apps (label
#           otel-libs/lang), waits for their pods to go, and keeps the
#           backing services.
#   delete --all  deletes everything labelled app.kubernetes.io/component=demo,
#           Secrets included. `delete` without an option refuses.
#   render  prints the manifests (Secrets, backing services, the apps of LANGS)
#           with <generated> in place of every secret value; works offline (no
#           kubectl, openssl or python3 call).
# Env:   KUBE_CONTEXT (k8s01), NAMESPACE (dev-otel-libs) — another target is
#        refused unless DEMO_ALLOW_OTHER_TARGET=1;
#        RUN (run id, default UTC yyyymmddthhmm; label otel-libs/run);
#        LANGS (apps of apply --all and render; default "go python node dotnet");
#        REGISTRY (harbor.staffops.space/development), TAG (default: demo-tag.sh,
#        the newest commit that touched an image input, as build-demo.sh tags;
#        no `latest` fallback, and `latest` is refused). With the default tag,
#        apply --lang/--all first checks read-only that every image exists
#        (`docker buildx imagetools inspect`); an explicit TAG skips the check;
#        DEMO_WATCH_SECONDS (FailedScheduling watch after the rollout, 60);
#        DEMO_ENV_FILE (apply and render: env overrides for the apps, task
#        DE13, design DE-D7 amendments 2026-11-19 and 2026-11-23). One
#        `<lang>/<app> NAME=VALUE` per line, `*` for either part, `#` comment
#        and blank lines skipped. A line replaces the value of a variable the
#        app already sets (same position) or adds a new one after them; two
#        lines setting one name for the same app are an error. Names: OTEL_*
#        and DEMO_* only; denied: OTEL_SERVICE_NAME, OTEL_RESOURCE_ATTRIBUTES,
#        DEMO_PG_*, DEMO_REDIS_* and DEMO_*_PASSWORD_FILE. Values: no `"`, `\`
#        or control character. Any bad line fails before a write, without
#        printing its value. `render` shows every *_HEADERS value as <set>.
#        DEMO_RESTART_NONCE=<value> is an allowed name the apps ignore: a new
#        value changes the pod template, so apply rolls the Deployment.
#        Without the file the manifests are byte-identical to the hook-less ones.
#        DEMO_RESTORE=1 (apply --lang/--all and scale only; the soak trap,
#        testing/k8s/soak): puts already-running apps back without the hook's
#        values, so the K-D2 capacity pre-flight is skipped (the apps already
#        hold their requests: a restore only removes env or returns to the
#        recorded replica count). Refused with DEMO_ENV_FILE, and refused when
#        any Deployment of those languages is not applied. In restore mode
#        `apply` also refuses when a target runs another image tag than TAG or
#        has replicas 0 (apply would set 1: a scale-up), and `scale` refuses
#        an n above the current replicas of every target (a scale-up runs the
#        normal pre-flight, without DEMO_RESTORE). The rollout and the
#        FailedScheduling watch still run.
set -euo pipefail
set +x # never trace: secret values pass through shell variables

here="$(cd "$(dirname "$0")" && pwd)"
action=${1:-apply}
ctx=${KUBE_CONTEXT:-k8s01}
ns=${NAMESPACE:-dev-otel-libs}
run=${RUN:-$(date -u +%Y%m%dt%H%M)}
run_explicit=${RUN:+yes}

# Docker Hub, pinned by multi-arch index digest (ruling DE-O4; read 2026-11-16).
pg_image=docker.io/library/postgres:17.11-alpine@sha256:e5d2edd28b1f8d40a0a6c0c9cdc545b19b56a0d5d88f94bc11a97d970d4700d0
redis_image=docker.io/library/redis:7.4.12-alpine@sha256:7f7d30b7fdd09467bac58198a1f166dd69789d348d4fe859cccb20e0cf287fbe

# The database is shared: every language's role exists whatever LANGS the
# apps later run with (DE-D12 rotation keeps PostgreSQL and Redis up).
langs="go python node dotnet"
selector="app.kubernetes.io/part-of=otel-libs,app.kubernetes.io/component=demo"

# Budget table (design DE-D12, calibrated by DE7 on 2026-11-17), defined
# once: the manifests and the pre-flight both read these.
pg_cpu_m=20 pg_memory_mi=64 pg_limit_mi=256
redis_cpu_m=25 redis_memory_mi=32 redis_limit_mi=128
run_cpu="$((pg_cpu_m + redis_cpu_m))m"
run_memory="$((pg_memory_mi + redis_memory_mi))Mi"
run_pods=2

# ── Apps (task DE4).
app_langs=${LANGS:-go python node dotnet}
registry=${REGISTRY:-harbor.staffops.space/development}
tag=${TAG:-}
tag_explicit=${TAG:+yes}
backing_created=""
watch_seconds=${DEMO_WATCH_SECONDS:-60}
agent=http://otel-agent-collector.opentelemetry.svc.cluster.local:4317
apps="worker api backend"
http_port=8080 grpc_port=50051

for lang in $app_langs; do
  case "$lang" in
    go|python|node|dotnet) ;;
    *) echo "demo: unknown language '$lang' in LANGS (go python node dotnet)" >&2; exit 2 ;;
  esac
done
[ -n "${app_langs// /}" ] || { echo "demo: LANGS is empty" >&2; exit 2; }
[[ "$watch_seconds" =~ ^[0-9]+$ ]] || { echo "demo: DEMO_WATCH_SECONDS must be a whole number" >&2; exit 2; }

# resolve_tag: the image tag, validated; derived lazily so that commands which
# render no app (delete, apply of the backing services) need no git checkout.
resolve_tag() {
  if [ -z "$tag" ]; then
    tag=$("$here/demo-tag.sh") || exit 2
  fi
  if ! [[ "$tag" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]] || [ "$tag" = latest ]; then
    echo "demo: TAG '$tag' is not a valid image tag (or is latest)" >&2
    exit 2
  fi
}
[ -z "$tag_explicit" ] || resolve_tag

# app_budget <lang>: per app pod, from the Budget table (DE-D12); sets
# app_cpu_m, app_memory_mi, app_limit_mi.
app_budget() {
  case "$1" in
    go)     app_cpu_m=5  app_memory_mi=32  app_limit_mi=64 ;;
    python) app_cpu_m=15 app_memory_mi=96  app_limit_mi=192 ;;
    node)   app_cpu_m=10 app_memory_mi=64  app_limit_mi=192 ;;
    dotnet) app_cpu_m=20 app_memory_mi=128 app_limit_mi=256 ;;
  esac
}

# app_user <lang>: the image's non-root user (Dockerfile USER), used as
# runAsUser, runAsGroup and fsGroup so the 0440 password files are readable.
app_user() {
  case "$1" in
    go)     echo 65534 ;; # USER 65534:65534
    python) echo 65532 ;; # USER 65532:65532
    node)   echo 1000 ;;  # USER node (node:*-slim, 1000:1000)
    dotnet) echo 1654 ;;  # USER $APP_UID (aspnet:10.0, 1654:1654)
  esac
}

# apps_requests <langs>: "<cpu>m <memory>Mi <pods>" summed over the apps.
apps_requests() {
  local lang cpu=0 mem=0 pods=0
  for lang in $1; do
    app_budget "$lang"
    cpu=$((cpu + 3 * app_cpu_m)) mem=$((mem + 3 * app_memory_mi)) pods=$((pods + 3))
  done
  echo "${cpu}m ${mem}Mi $pods"
}

if ! [[ "$run" =~ ^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$ ]]; then
  echo "demo: RUN must be 1-32 characters of [a-z0-9-], alphanumeric at both ends" >&2
  exit 2
fi

labels() { # <indent> <name>
  sed "s/^/$1/" <<YAML
app.kubernetes.io/name: $2
app.kubernetes.io/part-of: otel-libs
app.kubernetes.io/component: demo
otel-libs/run: "$run"
YAML
}

hardening() {
  cat <<YAML
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]
YAML
}

# pod_security <uid> <gid>
pod_security() {
  cat <<YAML
      automountServiceAccountToken: false
      securityContext:
        runAsNonRoot: true
        runAsUser: $1
        runAsGroup: $2
        fsGroup: $2
        seccompProfile:
          type: RuntimeDefault
YAML
}

# ── Secrets: printf only (no heredoc), so no secret byte goes to a temp file.
# Values come from the variables set by generate_credentials or render.

secret_header() { # <name>
  printf -- '---\napiVersion: v1\nkind: Secret\nmetadata:\n  name: %s\n  namespace: %s\n  labels:\n' "$1" "$ns"
  labels '    ' "$1"
  printf 'type: Opaque\nstringData:\n'
}

render_secrets() {
  local lang var
  secret_header demo-postgres-auth
  printf '  POSTGRES_PASSWORD: "%s"\n' "$pg_super_pw"
  for lang in $langs; do
    var="pw_$lang"
    printf '  demo_%s: "%s"\n' "$lang" "${!var}"
  done

  secret_header demo-postgres-initdb
  printf '  10-roles.sql: |\n'
  printf '    -- Generated by testing/k8s/demo/demo.sh for run %s (DE-D13).\n' "$run"
  printf '    -- log_min_error_statement=panic: a failing statement is not echoed to\n'
  printf '    -- the server log (psql may still print the failing line, a verifier,\n'
  printf '    -- never a password, to the container log: DE-D13).\n'
  printf '    SET log_min_error_statement = panic;\n'
  for lang in $langs; do
    var="verifier_$lang"
    printf "    CREATE ROLE demo_%s LOGIN PASSWORD '%s';\n" "$lang" "${!var}"
    printf '    CREATE DATABASE demo_%s OWNER demo_%s;\n' "$lang" "$lang"
    printf '    REVOKE CONNECT, TEMPORARY ON DATABASE demo_%s FROM PUBLIC;\n' "$lang"
    printf '    GRANT CONNECT ON DATABASE demo_%s TO demo_%s;\n' "$lang" "$lang"
  done
  printf '    -- Each role reaches its own database only.\n'
  printf '    REVOKE CONNECT ON DATABASE postgres FROM PUBLIC;\n'
  printf '    REVOKE CONNECT ON DATABASE template1 FROM PUBLIC;\n'
  printf '    RESET log_min_error_statement;\n'

  secret_header demo-redis-auth
  printf '  password: "%s"\n' "$redis_pw"
  printf '  redis.conf: |\n'
  printf '    bind * -::*\n    port 6379\n    protected-mode yes\n'
  printf '    requirepass %s\n' "$redis_pw"
  printf '    save ""\n    appendonly no\n    dir /data\n'
  printf '    maxmemory 64mb\n    maxmemory-policy noeviction\n'
}

# ── Workloads (no secret values; plain heredocs are fine).

render_postgres() {
  cat <<YAML
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: demo-postgres
  namespace: $ns
  labels:
$(labels '    ' demo-postgres)
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app.kubernetes.io/name: demo-postgres
  template:
    metadata:
      labels:
$(labels '        ' demo-postgres)
    spec:
$(pod_security 70 70)
      terminationGracePeriodSeconds: 30
      containers:
        - name: postgres
          image: $pg_image
          imagePullPolicy: IfNotPresent
          ports:
            - name: postgres
              containerPort: 5432
          env:
            # The superuser password is a file, not an env value (DE-D13 r3.3).
            - name: POSTGRES_PASSWORD_FILE
              value: /run/secrets/postgres/POSTGRES_PASSWORD
            - name: PGDATA
              value: /var/lib/postgresql/data/pgdata
          startupProbe:
            exec:
              command: ["pg_isready", "-q", "-h", "127.0.0.1", "-p", "5432", "-U", "postgres", "-d", "postgres"]
            periodSeconds: 5
            failureThreshold: 60
          readinessProbe:
            exec:
              command: ["pg_isready", "-q", "-h", "127.0.0.1", "-p", "5432", "-U", "postgres", "-d", "postgres"]
            periodSeconds: 10
          livenessProbe:
            tcpSocket: {port: 5432}
            periodSeconds: 20
          resources:
            requests: {cpu: ${pg_cpu_m}m, memory: ${pg_memory_mi}Mi}
            limits: {memory: ${pg_limit_mi}Mi}
$(hardening)
          volumeMounts:
            - {name: data, mountPath: /var/lib/postgresql/data}
            - {name: run, mountPath: /var/run/postgresql}
            - {name: tmp, mountPath: /tmp}
            - {name: initdb, mountPath: /docker-entrypoint-initdb.d, readOnly: true}
            - {name: auth, mountPath: /run/secrets/postgres, readOnly: true}
      volumes:
        - name: data
          emptyDir: {sizeLimit: 1Gi}
        - name: run
          emptyDir: {}
        - name: tmp
          emptyDir: {}
        - name: initdb
          secret:
            secretName: demo-postgres-initdb
            defaultMode: 0440
        - name: auth
          secret:
            secretName: demo-postgres-auth
            defaultMode: 0440
            items:
              - {key: POSTGRES_PASSWORD, path: POSTGRES_PASSWORD}
---
apiVersion: v1
kind: Service
metadata:
  name: demo-postgres
  namespace: $ns
  labels:
$(labels '    ' demo-postgres)
spec:
  selector:
    app.kubernetes.io/name: demo-postgres
  ports:
    - name: postgres
      port: 5432
      targetPort: 5432
      appProtocol: tcp
YAML
}

render_redis() {
  cat <<YAML
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: demo-redis
  namespace: $ns
  labels:
$(labels '    ' demo-redis)
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app.kubernetes.io/name: demo-redis
  template:
    metadata:
      labels:
$(labels '        ' demo-redis)
    spec:
$(pod_security 999 1000)
      terminationGracePeriodSeconds: 15
      containers:
        - name: redis
          image: $redis_image
          imagePullPolicy: IfNotPresent
          args: ["redis-server", "/etc/redis/redis.conf"]
          ports:
            - name: redis
              containerPort: 6379
          # The probes read the password from the mounted file into REDISCLI_AUTH
          # for redis-cli only: never argv, never a container env value (DE-D13 r3.3).
          readinessProbe:
            exec:
              command: ["sh", "-c", "REDISCLI_AUTH=\"\$(cat /etc/redis/password)\" redis-cli ping | grep -qx PONG"]
            periodSeconds: 10
          livenessProbe:
            exec:
              command: ["sh", "-c", "REDISCLI_AUTH=\"\$(cat /etc/redis/password)\" redis-cli ping | grep -qx PONG"]
            initialDelaySeconds: 10
            periodSeconds: 20
          resources:
            requests: {cpu: ${redis_cpu_m}m, memory: ${redis_memory_mi}Mi}
            limits: {memory: ${redis_limit_mi}Mi}
$(hardening)
          volumeMounts:
            - {name: data, mountPath: /data}
            - {name: tmp, mountPath: /tmp}
            - {name: conf, mountPath: /etc/redis, readOnly: true}
      volumes:
        - name: data
          emptyDir: {sizeLimit: 256Mi}
        - name: tmp
          emptyDir: {}
        - name: conf
          secret:
            secretName: demo-redis-auth
            defaultMode: 0440
            items:
              - {key: redis.conf, path: redis.conf}
              - {key: password, path: password}
---
apiVersion: v1
kind: Service
metadata:
  name: demo-redis
  namespace: $ns
  labels:
$(labels '    ' demo-redis)
spec:
  selector:
    app.kubernetes.io/name: demo-redis
  ports:
    - name: redis
      port: 6379
      targetPort: 6379
      appProtocol: tcp
YAML
}

render_workloads() {
  render_postgres
  render_redis
}

app_labels() { # <indent> <name> <lang>
  labels "$1" "$2"
  printf '%sotel-libs/lang: %s\n' "$1" "$3"
}

env_var() { # <name> <value>
  printf '            - name: %s\n              value: "%s"\n' "$1" "$2"
}

# ── Env-override hook (task DE13; design DE-D7 amendments 2026-11-19 and
# 2026-11-23). Parallel arrays, one entry per accepted DEMO_ENV_FILE line
# (bash 3.2 has no associative arrays): selector language and app ("*" = any),
# variable name, value.
ov_lang=() ov_app=() ov_name=() ov_value=() ov_line=()
env_mask="" # render: show every *_HEADERS value as <set>

# env_file_error <line> <message>: the value of a refused line is never printed.
env_file_error() {
  echo "demo: DEMO_ENV_FILE line $1: $2; nothing applied" >&2
  exit 2
}

# env_name_denied <name>: the identity the soak checks read (K2, K3, K5) and
# the DE-D13 credential wiring are not overridable.
env_name_denied() {
  case "$1" in
    OTEL_SERVICE_NAME|OTEL_RESOURCE_ATTRIBUTES|DEMO_PG_*|DEMO_REDIS_*|DEMO_*_PASSWORD_FILE) return 0 ;;
  esac
  return 1
}

# load_env_file <path>: parses and validates every line before anything renders.
load_env_file() {
  local file=$1 raw line n=0 l a name value i
  local re='^([a-z]+|\*)/([a-z]+|\*)[[:space:]]+([A-Za-z0-9_]+)=(.*)$'
  if [ ! -f "$file" ] || [ ! -r "$file" ]; then
    echo "demo: DEMO_ENV_FILE '$file' is not a readable file; nothing applied" >&2
    exit 2
  fi
  while IFS= read -r raw || [ -n "$raw" ]; do
    n=$((n + 1))
    line=${raw#"${raw%%[![:space:]]*}"} # leading whitespace
    case "$line" in ''|'#'*) continue ;; esac
    [[ "$line" =~ $re ]] || env_file_error "$n" "not '<lang>/<app> NAME=VALUE'"
    l=${BASH_REMATCH[1]} a=${BASH_REMATCH[2]} name=${BASH_REMATCH[3]} value=${BASH_REMATCH[4]}
    case "$l" in go|python|node|dotnet|'*') ;; *) env_file_error "$n" "unknown language '$l' (go python node dotnet *)" ;; esac
    case "$a" in worker|api|backend|'*') ;; *) env_file_error "$n" "unknown app '$a' (worker api backend *)" ;; esac
    [[ "$name" =~ ^(OTEL|DEMO)_[A-Z0-9_]+$ ]] || env_file_error "$n" "$name is not an OTEL_* or DEMO_* name"
    ! env_name_denied "$name" || env_file_error "$n" "$name may not be overridden (identity or credential wiring)"
    # The value goes into a YAML double-quoted scalar: no quote, backslash or control character.
    if [[ "$value" == *'"'* || "$value" == *'\'* || "$value" =~ [[:cntrl:]] ]]; then
      env_file_error "$n" "the value of $name holds a quote, a backslash or a control character"
    fi
    for ((i = 0; i < ${#ov_name[@]}; i++)); do
      if [ "${ov_name[i]}" = "$name" ] \
        && { [ "${ov_lang[i]}" = "$l" ] || [ "${ov_lang[i]}" = '*' ] || [ "$l" = '*' ]; } \
        && { [ "${ov_app[i]}" = "$a" ] || [ "${ov_app[i]}" = '*' ] || [ "$a" = '*' ]; }; then
        env_file_error "$n" "duplicate: $name is already set for an overlapping app by line ${ov_line[i]}"
      fi
    done
    ov_lang+=("$l") ov_app+=("$a") ov_name+=("$name") ov_value+=("$value") ov_line+=("$n")
  done <"$file"
}

# ov_matches <index> <lang> <app>
ov_matches() {
  { [ "${ov_lang[$1]}" = '*' ] || [ "${ov_lang[$1]}" = "$2" ]; } \
    && { [ "${ov_app[$1]}" = '*' ] || [ "${ov_app[$1]}" = "$3" ]; }
}

env_out() { # <name> <value>
  if [ -n "$env_mask" ] && [[ "$1" == *_HEADERS ]]; then
    env_var "$1" "<set>"
  else
    env_var "$1" "$2"
  fi
}

# emit_env <lang> <app> <name> <value> [...]: the app's defaults, each replaced
# in place by a matching override, then the overrides that add a variable.
emit_env() {
  local lang=$1 app=$2 name value i used=" "
  shift 2
  while [ $# -gt 0 ]; do
    name=$1 value=$2
    shift 2
    for ((i = 0; i < ${#ov_name[@]}; i++)); do
      if [ "${ov_name[i]}" = "$name" ] && ov_matches "$i" "$lang" "$app"; then
        value=${ov_value[i]} used="$used$i "
      fi
    done
    env_out "$name" "$value"
  done
  for ((i = 0; i < ${#ov_name[@]}; i++)); do
    if [[ "$used" != *" $i "* ]] && ov_matches "$i" "$lang" "$app"; then
      env_out "${ov_name[i]}" "${ov_value[i]}"
    fi
  done
}

# app_env <lang> <app>: DE-D2 telemetry settings plus the DE-D13 r3.3 wiring,
# with the DEMO_ENV_FILE overrides applied.
app_env() {
  local lang=$1 app=$2 defaults
  defaults=(
    OTEL_SERVICE_NAME "demo-$lang-$app"
    OTEL_RESOURCE_ATTRIBUTES "deployment.environment.name=dev,otel_libs.run=$run"
    OTEL_EXPORTER_OTLP_ENDPOINT "$agent"
    OTEL_EXPORTER_OTLP_PROTOCOL grpc
    OTEL_METRIC_EXPORT_INTERVAL 10000
  )
  case "$app" in
    worker)
      defaults+=(DEMO_API_URL "http://demo-$lang-api:$http_port"
        DEMO_REDIS_ADDR "demo-redis:6379"
        DEMO_REDIS_PASSWORD_FILE /run/secrets/demo-redis/password) ;;
    api)
      defaults+=(DEMO_HTTP_PORT "$http_port"
        DEMO_BACKEND_ADDR "demo-$lang-backend:$grpc_port"
        DEMO_REDIS_ADDR "demo-redis:6379"
        DEMO_REDIS_PASSWORD_FILE /run/secrets/demo-redis/password) ;;
    backend)
      defaults+=(DEMO_GRPC_PORT "$grpc_port"
        DEMO_PG_HOST demo-postgres
        DEMO_PG_DB "demo_$lang"
        DEMO_PG_USER "demo_$lang"
        DEMO_PG_PASSWORD_FILE /run/secrets/demo-postgres/password)
      # Without it the .NET gRPC server spans are not RPC-shaped (DE-D2).
      [ "$lang" != dotnet ] || defaults+=(OTEL_DOTNET_EXPERIMENTAL_ASPNETCORE_ENABLE_GRPC_INSTRUMENTATION true) ;;
  esac
  emit_env "$lang" "$app" "${defaults[@]}"
}

# app_probes <app>: startup and readiness on the app's own health check, a
# TCP liveness check (a backing-service outage must not restart the pod).
app_probes() {
  case "$1" in
    api) cat <<YAML
          ports:
            - name: http
              containerPort: $http_port
          startupProbe:
            httpGet: {path: /health, port: $http_port}
            periodSeconds: 5
            failureThreshold: 60
          readinessProbe:
            httpGet: {path: /health, port: $http_port}
            periodSeconds: 10
          livenessProbe:
            tcpSocket: {port: $http_port}
            periodSeconds: 20
            failureThreshold: 3
YAML
    ;;
    backend) cat <<YAML
          ports:
            - name: grpc
              containerPort: $grpc_port
          startupProbe:
            grpc: {port: $grpc_port}
            periodSeconds: 5
            failureThreshold: 60
          readinessProbe:
            grpc: {port: $grpc_port}
            periodSeconds: 10
          livenessProbe:
            tcpSocket: {port: $grpc_port}
            periodSeconds: 20
            failureThreshold: 3
YAML
    ;;
    worker) ;; # no listener: the worker's health is its traces (DE-D11)
  esac
}

# app_secret_mount <lang> <app>: least privilege — each app mounts only the
# password it uses, as a file (DE-D13 r3.3).
app_secret_mount() {
  case "$2" in
    worker|api) printf '            - {name: redis-auth, mountPath: /run/secrets/demo-redis, readOnly: true}\n' ;;
    backend)    printf '            - {name: pg-auth, mountPath: /run/secrets/demo-postgres, readOnly: true}\n' ;;
  esac
}

app_secret_volume() {
  case "$2" in
    worker|api) cat <<YAML
        - name: redis-auth
          secret:
            secretName: demo-redis-auth
            defaultMode: 0440
            items:
              - {key: password, path: password}
YAML
    ;;
    backend) cat <<YAML
        - name: pg-auth
          secret:
            secretName: demo-postgres-auth
            defaultMode: 0440
            items:
              - {key: demo_$1, path: password}
YAML
    ;;
  esac
}

render_app() { # <lang> <app>
  local lang=$1 app=$2 name="demo-$1-$2" uid
  app_budget "$lang"
  uid=$(app_user "$lang")
  cat <<YAML
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: $name
  namespace: $ns
  labels:
$(app_labels '    ' "$name" "$lang")
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
$(app_labels '        ' "$name" "$lang")
    spec:
$(pod_security "$uid" "$uid")
      terminationGracePeriodSeconds: 15
      containers:
        - name: $app
          image: $registry/otel-libs-demo-$lang-$app:$tag
          imagePullPolicy: IfNotPresent
          env:
$(app_env "$lang" "$app")
$(app_probes "$app")
          resources:
            requests: {cpu: ${app_cpu_m}m, memory: ${app_memory_mi}Mi}
            limits: {memory: ${app_limit_mi}Mi}
$(hardening)
          volumeMounts:
            - {name: tmp, mountPath: /tmp}
$(app_secret_mount "$lang" "$app")
      volumes:
        - name: tmp
          emptyDir: {sizeLimit: 64Mi}
$(app_secret_volume "$lang" "$app")
YAML
  case "$app" in
    api) render_app_service "$lang" "$name" http "$http_port" ;;
    backend) render_app_service "$lang" "$name" grpc "$grpc_port" ;;
  esac
}

render_app_service() { # <lang> <name> <port name> <port>
  cat <<YAML
---
apiVersion: v1
kind: Service
metadata:
  name: $2
  namespace: $ns
  labels:
$(app_labels '    ' "$2" "$1")
spec:
  selector:
    app.kubernetes.io/name: $2
  ports:
    - name: $3
      port: $4
      targetPort: $4
      appProtocol: $3
YAML
}

render_apps() { # <langs>
  local lang app
  for lang in $1; do
    for app in $apps; do render_app "$lang" "$app"; done
  done
}

placeholder_credentials() {
  local lang
  pg_super_pw="<generated>"
  redis_pw="<generated>"
  for lang in $langs; do
    printf -v "pw_$lang" '%s' "<generated>"
    printf -v "verifier_$lang" '%s' "<generated>"
  done
}

# 48 hex characters per password (openssl rand -hex 24), kept in variables;
# each role's verifier is computed from the password on stdin (DE-D13).
generate_credentials() {
  local lang pw verifier
  pg_super_pw=$(openssl rand -hex 24)
  redis_pw=$(openssl rand -hex 24)
  for lang in $langs; do
    pw=$(openssl rand -hex 24)
    verifier=$(printf '%s\n' "$pw" | python3 -I "$here/scram.py")
    printf -v "pw_$lang" '%s' "$pw"
    printf -v "verifier_$lang" '%s' "$verifier"
  done
  pw="" verifier=""
  local v hex='^[0-9a-f]{48}$'
  # shellcheck disable=SC2016 # literal $ in the regex
  local scram='^SCRAM-SHA-256\$4096:[A-Za-z0-9+/]{22}==\$[A-Za-z0-9+/]{43}=:[A-Za-z0-9+/]{43}=$'
  for v in pg_super_pw redis_pw $(for lang in $langs; do printf 'pw_%s ' "$lang"; done); do
    [[ "${!v}" =~ $hex ]] || { echo "demo: password generation failed ($v)" >&2; exit 1; }
  done
  for lang in $langs; do
    v="verifier_$lang"
    [[ "${!v}" =~ $scram ]] || { echo "demo: verifier generation failed ($v)" >&2; exit 1; }
  done
}

forget_credentials() {
  local lang
  unset pg_super_pw redis_pw
  for lang in $langs; do unset "pw_$lang" "verifier_$lang"; done
}

k() { kubectl --context "$ctx" -n "$ns" "$@"; }

guard_target() {
  if [ "$ctx/$ns" != "k8s01/dev-otel-libs" ] && [ "${DEMO_ALLOW_OTHER_TARGET:-0}" != 1 ]; then
    echo "demo: refusing target context '$ctx', namespace '$ns' (expected k8s01/dev-otel-libs; set DEMO_ALLOW_OTHER_TARGET=1 to override)" >&2
    exit 2
  fi
}

# The initdb script runs once, on an empty PGDATA. A container restart during
# init leaves PGDATA half-initialized, and the entrypoint then skips the script.
verify_init() {
  local restarts roles
  restarts=$(k get pods -l app.kubernetes.io/name=demo-postgres -o jsonpath='{range .items[*]}{range .status.containerStatuses[*]}{.restartCount}{" "}{end}{end}')
  roles=$(k exec deploy/demo-postgres -- psql -X -q -U postgres -d postgres -tAc \
    "select count(*) from pg_roles where rolname in ('demo_go','demo_python','demo_node','demo_dotnet')" 2>/dev/null || echo error)
  roles=${roles//[[:space:]]/}
  if [ -z "${restarts// /}" ] || [ -n "$(tr -d '0 ' <<<"$restarts")" ] || [ "$roles" != 4 ]; then
    echo "demo: PostgreSQL init check failed (restarts: ${restarts:-none}; demo roles: $roles of 4): PGDATA may be half-initialized; run '$0 delete --all' and apply again" >&2
    exit 1
  fi
  echo "demo: PostgreSQL init verified (no restart, 4 demo roles)"
}

apply_backing() {
  kubectl --context "$ctx" get namespace "$ns" -o name >/dev/null

  local present
  present=$(k get deployments -l "$selector,!otel-libs/lang" -o jsonpath='{range .items[*]}{.metadata.name}={.metadata.labels.otel-libs/run}{"\n"}{end}')
  if [ -n "$present" ]; then
    local pg redis
    pg=$(grep '^demo-postgres=' <<<"$present" || true)
    redis=$(grep '^demo-redis=' <<<"$present" || true)
    if [ -n "$pg" ] && [ -n "$redis" ] && k get secret demo-postgres-auth demo-postgres-initdb demo-redis-auth -o name >/dev/null 2>&1; then
      if [ "${pg#*=}" != "${redis#*=}" ]; then
        echo "demo: demo-postgres (run ${pg#*=}) and demo-redis (run ${redis#*=}) disagree; run '$0 delete --all' first" >&2
        exit 1
      fi
      if [ -n "$run_explicit" ] && [ "$run" != "${pg#*=}" ]; then
        echo "demo: RUN=$run differs from the running backing services (run ${pg#*=}); unset RUN to reuse them, or '$0 delete --all' first" >&2
        exit 1
      fi
      run=${pg#*=}
      echo "demo: backing services already running (run $run); nothing created"
      verify_init
      echo "demo: run=$run"
      return 0
    fi
    echo "demo: partial demo state in $ns (deployments: $(tr '\n' ' ' <<<"$present")); run '$0 delete --all' first" >&2
    exit 1
  fi

  echo "demo: pre-flight (soak K-D2) for run $run"
  python3 -I "$here/preflight.py" --context "$ctx" --cpu "$run_cpu" --memory "$run_memory" --pods "$run_pods" || {
    echo "demo: pre-flight failed; nothing applied" >&2
    exit 1
  }

  # Render everything before writing anything.
  local workloads secrets
  workloads=$(render_workloads)
  generate_credentials
  secrets=$(render_secrets)
  forget_credentials

  # A crashed earlier apply may have left Secrets behind (DE-D13).
  k delete secret demo-postgres-auth demo-postgres-initdb demo-redis-auth --ignore-not-found
  printf '%s\n' "$secrets" | kubectl --context "$ctx" create -f -
  secrets=""
  printf '%s\n' "$workloads" | kubectl --context "$ctx" apply --server-side --field-manager=otel-libs-demo -f -
  backing_created=yes
  k rollout status deployment/demo-postgres deployment/demo-redis --timeout=420s
  verify_init
  echo "demo: run=$run"
}

# scale_apps_down <selector>: the K-D2 reaction to a platform FailedScheduling.
scale_apps_down() {
  echo "demo: a platform pod failed to schedule after this run started; scaling the apps of this apply to 0" >&2
  k scale deployment -l "$1" --replicas=0 || true
}

# sched_check <since> <selector>: one K-D2 FailedScheduling look-up; counts
# the look-ups and the blind ones (schedwatch exit 2) in sched_checks/sched_blind.
sched_checks=0 sched_blind=0
sched_check() {
  local rc=0
  sched_checks=$((sched_checks + 1))
  python3 -I "$here/schedwatch.py" --context "$ctx" --namespace "$ns" --since "$1" || rc=$?
  case "$rc" in
    0) ;;
    10) scale_apps_down "$2"; exit 1 ;;
    *) sched_blind=$((sched_blind + 1))
       echo "demo: FailedScheduling watch could not look (exit $rc)" >&2 ;;
  esac
}

# sched_verdict: a watch that never looked proves nothing, so apply fails.
sched_verdict() {
  if [ "$sched_checks" -gt 0 ] && [ "$sched_blind" -eq "$sched_checks" ]; then
    echo "demo: the FailedScheduling watch was blind for all $sched_checks look-ups; the apps stay up, but this apply is not proven safe for the node — check events by hand or '$0 delete --lang <lang>'" >&2
    exit 1
  fi
  if [ "$sched_blind" -gt 0 ]; then
    echo "demo: warning: the FailedScheduling watch was blind for $sched_blind of $sched_checks look-ups" >&2
  fi
}

# backing_note: said whenever apps are not applied, so the reader knows what
# this run did create.
backing_note() {
  if [ -n "$backing_created" ]; then
    echo "demo: note: the backing services were created by this apply (run $run) and stay up; the apps were not applied" >&2
  fi
}

# check_images <langs>: read-only existence check of every app image (skipped
# for an explicit TAG: the caller vouches for it).
check_images() {
  local lang app image missing=""
  [ -z "$tag_explicit" ] || return 0
  for lang in $1; do
    for app in $apps; do
      image="$registry/otel-libs-demo-$lang-$app:$tag"
      docker buildx imagetools inspect "$image" >/dev/null 2>&1 || missing="$missing $image"
    done
  done
  if [ -n "$missing" ]; then
    echo "demo: image(s) not found (or not readable) for the default tag $tag:$missing" >&2
    echo "demo: build them with testing/k8s/build-demo.sh, or set TAG explicitly; nothing applied" >&2
    exit 1
  fi
  echo "demo: images present for tag $tag"
}

# app_deploys <langs>: the demo app Deployment names of those languages.
app_deploys() {
  local lang app out=""
  for lang in $1; do
    for app in $apps; do out="$out demo-$lang-$app"; done
  done
  echo "$out"
}

# lang_csv <langs>: "go,python" for an `otel-libs/lang in (...)` selector,
# which matches only the demo apps (the backing services carry no lang label).
lang_csv() {
  local lang req=""
  for lang in $1; do req="$req${req:+,}$lang"; done
  echo "$req"
}

# await_rollout <deploys> <selector> <since>: waits up to 600 s for the
# Deployments, with the K-D2 FailedScheduling watch during the rollout and
# DEMO_WATCH_SECONDS after it.
await_rollout() {
  local deploys=$1 sel=$2 since=$3 d deadline pending
  deadline=$((SECONDS + 600))
  while :; do
    sched_check "$since" "$sel"
    pending=""
    for d in $deploys; do
      k rollout status "deployment/$d" --timeout=1s >/dev/null 2>&1 || pending="$pending $d"
    done
    [ -n "$pending" ] || break
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "demo: rollout not complete after 600 s:$pending" >&2
      k get pods -l "$sel" -o wide >&2 || true
      exit 1
    fi
    sleep 15
  done
  echo "demo: apps rolled out:$deploys"
  deadline=$((SECONDS + watch_seconds))
  while [ "$SECONDS" -lt "$deadline" ]; do
    sleep 15
    sched_check "$since" "$sel"
  done
  k get pods -l "$sel" -o wide
  sched_verdict
}

# apply_apps <langs>: the apps of the given languages on the running (or just
# created) backing services, with their run id.
apply_apps() {
  local sel_langs=$1 cpu mem pods since sel lang_list
  resolve_tag
  check_images "$sel_langs"
  apply_backing

  read -r cpu mem pods <<<"$(apps_requests "$sel_langs")"
  if [ "$restore" = 1 ]; then
    require_applied "$sel_langs"
    restore_check_apply "$sel_langs"
    echo "demo: restore of the running apps of: $sel_langs (DEMO_RESTORE=1: no K-D2 pre-flight; run $run, image tag $tag)"
  else
  echo "demo: pre-flight (soak K-D2) for the apps of: $sel_langs (run $run, image tag $tag)"
  if ! python3 -I "$here/preflight.py" --context "$ctx" --cpu "$cpu" --memory "$mem" --pods "$pods"; then
    read -r -a lang_list <<<"$sel_langs"
    if [ "${#lang_list[@]}" -gt 1 ]; then
      echo "demo: the apps of '$sel_langs' do not fit; nothing applied. Rotate one language per window (DE-O8): $0 delete --lang <previous>; $0 apply --lang <lang>" >&2
    else
      echo "demo: pre-flight failed for $sel_langs; nothing applied" >&2
    fi
    backing_note
    exit 1
  fi
  fi

  sel="$selector,otel-libs/run=$run,otel-libs/lang in ($(lang_csv "$sel_langs"))"
  since=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  # --force-conflicts: `scale` leaves .spec.replicas owned by the kubectl field
  # manager; this apply takes it back (replicas 1) instead of failing.
  render_apps "$sel_langs" | kubectl --context "$ctx" apply --server-side --force-conflicts --field-manager=otel-libs-demo -f -
  await_rollout "$(app_deploys "$sel_langs")" "$sel" "$since"
  echo "demo: run=$run langs=$sel_langs"
}

# scale_apps <langs> <n>: the demo app Deployments of those languages to n
# replicas (task DE13; the backing services are never scaled).
# restore_state <langs>: "<name> <replicas> <image>" per app Deployment.
restore_state() {
  k get deployments -l "$selector,otel-libs/lang in ($(lang_csv "$1"))" \
    -o jsonpath='{range .items[*]}{.metadata.name} {.spec.replicas} {.spec.template.spec.containers[0].image}{"\n"}{end}'
}

# restore_check_apply <langs>: DEMO_RESTORE=1 apply only re-applies running
# apps at their running tag (never a scale-up from 0, never another tag).
restore_check_apply() {
  local name n image
  while read -r name n image; do
    [ -n "$name" ] || continue
    if [ "$n" = 0 ]; then
      echo "demo: DEMO_RESTORE=1: $name has replicas 0 (apply would scale it up); nothing applied" >&2
      exit 1
    fi
    if [ "${image##*:}" != "$tag" ]; then
      echo "demo: DEMO_RESTORE=1: $name runs image tag ${image##*:}, not TAG=$tag; nothing applied" >&2
      exit 1
    fi
  done <<<"$(restore_state "$1")"
}

# restore_check_scale <langs> <n>: DEMO_RESTORE=1 scale never goes above the
# current replicas of any target (a scale-up needs the K-D2 pre-flight).
restore_check_scale() {
  local name cur image above=""
  while read -r name cur image; do
    [ -n "$name" ] || continue
    [ "$2" -gt "${cur:-0}" ] && above="$above $name"
  done <<<"$(restore_state "$1")"
  [ -z "$above" ] || {
    echo "demo: DEMO_RESTORE=1: $2 replica(s) is above the current replicas of:$above; scale up without DEMO_RESTORE; nothing scaled" >&2
    exit 1
  }
}

# require_applied <langs>: every demo app Deployment of those languages exists.
require_applied() {
  local sel_langs=$1 sel present d
  sel="$selector,otel-libs/lang in ($(lang_csv "$sel_langs"))"
  present=$(k get deployments -l "$sel" -o name)
  for d in $(app_deploys "$sel_langs"); do
    grep -qxF "deployment.apps/$d" <<<"$present" || {
      echo "demo: $d is not applied; run '$0 apply --lang <lang>' first; nothing changed" >&2
      exit 1
    }
  done
}

scale_apps() {
  local sel_langs=$1 n=$2 sel since deploys cpu mem pods
  sel="$selector,otel-libs/lang in ($(lang_csv "$sel_langs"))"
  deploys=$(app_deploys "$sel_langs")
  require_applied "$sel_langs"
  if [ "$n" -eq 0 ]; then
    k scale deployment -l "$sel" --replicas=0
    if [ -n "$(k get pods -l "$sel" -o name)" ]; then
      k wait --for=delete pods -l "$sel" --timeout=120s
    fi
    echo "demo: scaled to 0: $sel_langs"
    return 0
  fi
  if [ "$restore" = 1 ]; then
    restore_check_scale "$sel_langs" "$n"
    echo "demo: restore to $n replica(s) of the apps of: $sel_langs (DEMO_RESTORE=1: no K-D2 pre-flight)"
  else
  read -r cpu mem pods <<<"$(apps_requests "$sel_langs")"
  cpu=$(( ${cpu%m} * n ))m mem=$(( ${mem%Mi} * n ))Mi pods=$((pods * n))
  echo "demo: pre-flight (soak K-D2) for $n replica(s) of the apps of: $sel_langs"
  python3 -I "$here/preflight.py" --context "$ctx" --cpu "$cpu" --memory "$mem" --pods "$pods" || {
    echo "demo: pre-flight failed for $n replica(s) of $sel_langs; nothing scaled" >&2
    exit 1
  }
  fi
  since=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  k scale deployment -l "$sel" --replicas="$n"
  await_rollout "$deploys" "$sel" "$since"
  echo "demo: scaled to $n: $sel_langs"
}

# scale_count <n>: a whole number 0-999 without a leading zero.
scale_count() {
  [[ "${1:-}" =~ ^(0|[1-9][0-9]{0,2})$ ]] || {
    echo "demo: scale needs a replica count 0-999 (got '${1:-}')" >&2
    exit 2
  }
}

case "$action" in render|apply) [ -z "${DEMO_ENV_FILE:-}" ] || load_env_file "$DEMO_ENV_FILE" ;; esac

restore=${DEMO_RESTORE:-}
case "$restore" in
  "") ;;
  1)
    case "$action ${2:-}" in
      "apply --all"|"apply --lang"|"scale --all"|"scale --lang") ;;
      *) echo "demo: DEMO_RESTORE=1 applies only to apply --lang/--all and scale; nothing done" >&2; exit 2 ;;
    esac
    [ -z "${DEMO_ENV_FILE:-}" ] || { echo "demo: DEMO_RESTORE=1 removes the hook's values: it refuses DEMO_ENV_FILE; nothing done" >&2; exit 2; } ;;
  *) echo "demo: DEMO_RESTORE must be 1 or unset (got '$restore')" >&2; exit 2 ;;
esac

case "$action" in
  render)
    env_mask=yes
    placeholder_credentials
    render_secrets
    render_workloads
    resolve_tag
    render_apps "$app_langs"
    ;;
  apply)
    guard_target
    case "${2:-}" in
      "") apply_backing ;;
      --all) apply_apps "$app_langs" ;;
      --lang)
        case "${3:-}" in
          go|python|node|dotnet) apply_apps "$3" ;;
          *) echo "demo: apply --lang needs one of go python node dotnet" >&2; exit 2 ;;
        esac ;;
      *) echo "demo: apply takes no option (backing services), --lang <lang> or --all" >&2; exit 2 ;;
    esac ;;
  scale)
    guard_target
    case "${2:-}" in
      --all) scale_count "${3:-}"; [ $# -eq 3 ] || { echo "demo: scale --all takes one count" >&2; exit 2; }
        scale_apps "$app_langs" "$3" ;;
      --lang)
        case "${3:-}" in
          go|python|node|dotnet) scale_count "${4:-}"; [ $# -eq 4 ] || { echo "demo: scale --lang takes one language and one count" >&2; exit 2; }
            scale_apps "$3" "$4" ;;
          *) echo "demo: scale --lang needs one of go python node dotnet" >&2; exit 2 ;;
        esac ;;
      *) echo "demo: scale needs --lang <lang> <n> or --all <n>" >&2; exit 2 ;;
    esac ;;
  delete)
    guard_target
    case "${2:-}" in
      --all) k delete deployments,services,secrets -l "$selector" --ignore-not-found --wait ;;
      --lang)
        case "${3:-}" in
          go|python|node|dotnet)
            k delete deployments,services -l "$selector,otel-libs/lang=$3" --ignore-not-found --wait
            # The Deployments are gone; wait for their pods too, so a rotation
            # window's pre-flight does not count them.
            if [ -n "$(k get pods -l "$selector,otel-libs/lang=$3" -o name)" ]; then
              k wait --for=delete pods -l "$selector,otel-libs/lang=$3" --timeout=120s
            fi ;;
          *) echo "demo: delete --lang needs one of go python node dotnet" >&2; exit 2 ;;
        esac ;;
      *) echo "demo: delete needs --lang <lang> (apps only) or --all (backing services and Secrets too)" >&2; exit 2 ;;
    esac ;;
  *) echo "usage: $0 apply [--lang <lang> | --all] | scale --lang <lang> <n> | scale --all <n> | delete --lang <lang> | delete --all | render" >&2; exit 2 ;;
esac
