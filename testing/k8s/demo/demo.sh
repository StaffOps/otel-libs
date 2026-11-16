#!/usr/bin/env bash
# Demo environment on the test cluster (specs/demo-environment). Task DE2:
# the shared backing services — one PostgreSQL and one Redis for every
# language (DE-D4), credentials generated per run and never printed (DE-D13),
# requests from the Budget table (DE-D12). The demo apps join in task DE4.
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
#
# Usage: testing/k8s/demo/demo.sh apply | delete --lang <lang> | delete --all | render
#   apply   K-D2 pre-flight, then creates the Secrets (stringData through
#           `kubectl create -f -`; never client-side apply, which stores the
#           plaintext in the last-applied annotation, never --from-literal)
#           and server-side applies the Deployments and Services; after the
#           rollout it checks the init (no PostgreSQL restart, the four roles
#           exist). If the backing services already run, it creates nothing,
#           re-checks the init and prints their run id: passwords live for the
#           backing services' lifetime (apply -> delete --all), and language
#           rotation windows reuse them (DE-D13). An explicit RUN that differs
#           from the running services' run is an error.
#   delete --lang <lang>  deletes that language's demo apps (label
#           otel-libs/lang; added by task DE4) and keeps the backing services.
#   delete --all  deletes everything labelled app.kubernetes.io/component=demo,
#           Secrets included. `delete` without an option refuses.
#   render  prints the manifests with <generated> in place of every secret
#           value; works offline (no kubectl, openssl or python3 call).
# Env:   KUBE_CONTEXT (k8s01), NAMESPACE (dev-otel-libs) — another target is
#        refused unless DEMO_ALLOW_OTHER_TARGET=1;
#        RUN (run id, default UTC yyyymmddthhmm; label otel-libs/run).
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

# Budget table (design DE-D12), defined once: the manifests and the
# pre-flight both read these.
pg_cpu_m=100 pg_memory_mi=256 pg_limit_mi=512
redis_cpu_m=50 redis_memory_mi=64 redis_limit_mi=128
run_cpu="$((pg_cpu_m + redis_cpu_m))m"
run_memory="$((pg_memory_mi + redis_memory_mi))Mi"
run_pods=2

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
YAML
}

render_workloads() {
  render_postgres
  render_redis
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

apply() {
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
  k rollout status deployment/demo-postgres deployment/demo-redis --timeout=420s
  verify_init
  echo "demo: run=$run"
}

case "$action" in
  render)
    placeholder_credentials
    render_secrets
    render_workloads
    ;;
  apply) guard_target; apply ;;
  delete)
    guard_target
    case "${2:-}" in
      --all) k delete deployments,services,secrets -l "$selector" --ignore-not-found --wait ;;
      --lang)
        case "${3:-}" in
          go|python|node|dotnet) k delete deployments,services -l "$selector,otel-libs/lang=$3" --ignore-not-found --wait ;;
          *) echo "demo: delete --lang needs one of go python node dotnet" >&2; exit 2 ;;
        esac ;;
      *) echo "demo: delete needs --lang <lang> (apps only) or --all (backing services and Secrets too)" >&2; exit 2 ;;
    esac ;;
  *) echo "usage: $0 apply | delete --lang <lang> | delete --all | render" >&2; exit 2 ;;
esac
