#!/usr/bin/env bash
# Unit tests of the Python demo apps (specs/demo-environment DE3b), Docker only.
#   python/demo/run-tests.sh      # the images' python:3.11-slim digest (linux/amd64)
# Installs the hashed lock and the local helper in a throwaway container (the
# repo is mounted read-only) and runs pytest with the 90 % coverage gate over
# the app modules.
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
PY_IMAGE="${PY_IMAGE:-python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535}"
docker run --rm --platform linux/amd64 -e PIP_DISABLE_PIP_VERSION_CHECK=1 -e PIP_ROOT_USER_ACTION=ignore \
  -e PYTHONDONTWRITEBYTECODE=1 -e COVERAGE_FILE=/tmp/.coverage -v "$root:/repo:ro" "$PY_IMAGE" sh -ec '
    pip install -q --require-hashes --no-deps -r /repo/python/demo/requirements.lock
    cp -r /repo/python /tmp/helper && rm -rf /tmp/helper/demo /tmp/helper/tests /tmp/helper/example
    pip install -q --no-deps --no-build-isolation /tmp/helper
    pip install -q "pytest==8.4.2" "pytest-cov==7.0.0"
    pip check
    cd /repo/python/demo
    python -m pytest -q -p no:cacheprovider tests \
      --cov=democommon --cov=worker --cov=api --cov=backend \
      --cov-report=term-missing --cov-fail-under=90 \
      -o cache_dir=/tmp/pytest-cache --cov-config=/dev/null
  '
