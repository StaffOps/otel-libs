FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml /tmp/staffops-otel-helper/
COPY staffops_otel_helper/ /tmp/staffops-otel-helper/staffops_otel_helper/
COPY otel_helper/ /tmp/staffops-otel-helper/otel_helper/
RUN pip install --no-cache-dir /tmp/staffops-otel-helper

COPY example/python-process/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY example/python-process/ /app/

CMD ["python", "workers.py"]
