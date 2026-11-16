# syntax=docker/dockerfile:1
# Python demo worker (load driver + queue consumer) — specs/demo-environment DE3b.
# Build from the repository root (the context must see python/ and demo/):
#   docker build --platform linux/amd64 -f python/demo/worker/Dockerfile -t otel-libs-demo-python-worker:dev .
# Dockerfile.dockerignore next to this file limits the context to what is copied.

# python:3.11-slim (the steering pin; same as python/example), by digest.
ARG PYTHON_IMAGE=python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535

FROM ${PYTHON_IMAGE} AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
# Every third-party package from the hashed lock; nothing else is resolved.
COPY python/demo/requirements.lock /tmp/requirements.lock
RUN pip install --require-hashes --no-deps -r /tmp/requirements.lock
# The helper from the local tree, without dependencies (they are in the lock).
COPY python/pyproject.toml /tmp/helper/
COPY python/staffops_otel_helper/ /tmp/helper/staffops_otel_helper/
COPY python/otel_helper/ /tmp/helper/otel_helper/
# --no-build-isolation: the build backend (hatchling) comes from the hashed lock.
RUN pip install --no-deps --no-build-isolation /tmp/helper && pip check
# Stubs from the shared proto (DE-D3), flat modules demo_pb2 / demo_pb2_grpc.
COPY demo/proto/ /tmp/proto/
RUN mkdir -p /app \
 && python -m grpc_tools.protoc -I/tmp/proto/otellibs/demo/v1 \
      --python_out=/app --grpc_python_out=/app /tmp/proto/otellibs/demo/v1/demo.proto \
 && pip uninstall -y -q grpcio-tools setuptools hatchling pathspec pluggy trove-classifiers && pip check
# No SQL in this app: without SQLAlchemy the helper's deprecated implicit SQL
# instrumentation has nothing to turn on (and nothing to warn about).
RUN pip uninstall -y -q sqlalchemy greenlet psycopg psycopg-binary opentelemetry-instrumentation-sqlalchemy \
 && pip check
COPY python/demo/common/democommon.py python/demo/worker/worker.py /app/

# Bytecode at build time: the container may run with a read-only root filesystem.
RUN python -m compileall -q /opt/venv /app

FROM ${PYTHON_IMAGE}
ENV PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY --from=build /opt/venv /opt/venv
COPY --from=build /app /app
WORKDIR /app
USER 65532:65532

ENTRYPOINT ["python", "worker.py"]
