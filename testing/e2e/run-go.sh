# Runs the Go smoke program once per combination (inside the go-smoke service).
set -e
. ./combos.sh
# -buildvcs=false: the repo is mounted read-only and owned by another uid, so
# git refuses it ("dubious ownership") and VCS stamping fails the build.
(cd go-smoke && go build -buildvcs=false -o /tmp/go-smoke .)
echo "$COMBOS" | while IFS='|' read -r name envs; do
  # shellcheck disable=SC2086 # $envs is a list of VAR=value words
  env OTEL_SERVICE_NAME="e2e-go-$name" ENVIRONMENT=DEV OTEL_METRIC_EXPORT_INTERVAL=1000 $envs \
    /tmp/go-smoke || echo "go-smoke[$name] exited $? (see smoke.sh expectations)"
done
