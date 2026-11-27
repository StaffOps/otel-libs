# Runs the Node smoke program once per combination (inside the node-smoke service).
set -eu
# shellcheck disable=SC3040 # dash (node:*-slim /bin/sh) supports pipefail since 0.5.11
(set -o pipefail) 2>/dev/null && set -o pipefail
. ./combos.sh
# Copy out of the read-only mount without host node_modules/dist (they may hold
# another platform's files), as the node-tests service does; lifecycle scripts
# never run (--ignore-scripts, also set in nodejs/.npmrc).
mkdir -p /work
tar -C /src/nodejs --exclude=node_modules --exclude=dist --exclude=coverage --exclude='*.tgz' -cf /tmp/nodejs.tar .
tar -C /work -xf /tmp/nodejs.tar
[ -f /work/package.json ] || { echo "run-node: workspace copy failed (no /work/package.json)" >&2; exit 1; }
cd /work
# The whole workspace is installed: the core's build needs typescript and
# @types/node, which are root devDependencies (`npm ci --workspace` skips them).
npm ci --ignore-scripts --no-audit --no-fund >/dev/null
npm run --silent build --workspace=@staffops/otel-helper
cp /src/testing/e2e/node-smoke/smoke.cjs /work/node-smoke.cjs
echo "$COMBOS" | while IFS='|' read -r name envs; do
  # Bounded run (60 s, as run-go.sh): a hung export must not hang the job.
  # shellcheck disable=SC2086 # $envs is a list of VAR=value words
  env OTEL_SERVICE_NAME="e2e-node-$name" ENVIRONMENT=DEV OTEL_METRIC_EXPORT_INTERVAL=1000 \
    OTEL_HELPER_METRICS_PORT=0 $envs \
    timeout 60 node node-smoke.cjs || echo "node-smoke[$name] exited $? (see smoke.sh expectations)"
done
