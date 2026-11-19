# syntax=docker/dockerfile:1
# Python e2e smoke image for a Kubernetes Job. The repo has no Python smoke
# program (run-python.sh drives the long-running python-process example), so
# testing/k8s/images/python-smoke.py stands in, with the same markers as the
# Go, Node and .NET smoke programs.
# Build context: the repo tree root (testing/k8s/build-images.sh exports HEAD).
FROM python:3.11-slim AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
RUN python -m venv /opt/venv
COPY python/ /tmp/python/
# The helper core from source (no extras: the smoke needs no AWS/Redis/SQL).
RUN /opt/venv/bin/pip install -q /tmp/python

FROM python:3.11-slim
ENV PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY --from=build /opt/venv /opt/venv
COPY testing/k8s/images/python-smoke.py /app/python-smoke.py
USER 65532:65532
WORKDIR /app
# One run: span e2e-smoke-span, counter e2e.smoke.runs, WARN log e2e-smoke-log.
# Exits 1 when shutdown_telemetry raises or an OTLP exporter logs an ERROR
# record (the Python SDK reports a refused export only in its log; measured:
# endpoint http://127.0.0.1:9, grpc and http/protobuf), like the other smoke programs.
ENTRYPOINT ["python", "/app/python-smoke.py"]
