# Runs the Go smoke program once per combination (inside the go-smoke service).
set -e
. ./combos.sh
(cd go-smoke && go build -o /tmp/go-smoke .)
echo "$COMBOS" | while IFS='|' read -r name envs; do
  # shellcheck disable=SC2086 # $envs is a list of VAR=value words
  env OTEL_SERVICE_NAME="e2e-go-$name" ENVIRONMENT=DEV OTEL_METRIC_EXPORT_INTERVAL=1000 $envs \
    /tmp/go-smoke || echo "go-smoke[$name] exited $? (see smoke.sh expectations)"
done
