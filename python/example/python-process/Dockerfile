FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml /tmp/otel-helper/
COPY otel_helper/ /tmp/otel-helper/otel_helper/
RUN pip install --no-cache-dir /tmp/otel-helper

COPY example/python-process/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY example/python-process/ /app/

CMD ["python", "workers.py"]
