# Build from the repo root (context python/): docker build -t python-custom-signals -f python/example/python-custom-signals/Dockerfile python
FROM python:3.11-slim

WORKDIR /app

# Install otel-helper from local source
COPY pyproject.toml /tmp/otel-helper/
COPY otel_helper/ /tmp/otel-helper/otel_helper/
RUN pip install --no-cache-dir /tmp/otel-helper

COPY example/python-custom-signals/custom_signals.py /app/

USER 65534
CMD ["python", "custom_signals.py"]
