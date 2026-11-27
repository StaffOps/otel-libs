# Runs the python-process example once per combination (inside python-smoke).
set -e
. ./combos.sh
cp -r /src/python /work && cd /work
pip install -q . -r example/python-process/requirements.txt
cd example/python-process
echo "$COMBOS" | while IFS='|' read -r name envs; do
  # Bounded run: spans/metrics/logs are exported on their schedules inside the
  # window; `timeout` exiting 124 is the expected outcome.
  # shellcheck disable=SC2086
  env OTEL_SERVICE_NAME="e2e-python-$name" ENVIRONMENT=DEV OTEL_METRIC_EXPORT_INTERVAL=1000 \
    OTEL_HELPER_METRICS_PORT=0 API_URL=http://127.0.0.1:9 $envs timeout 12 python workers.py >/dev/null 2>&1 \
    || [ $? -eq 124 ] || echo "python-process[$name] failed"
done
