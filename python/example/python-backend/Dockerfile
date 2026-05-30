FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml /tmp/otel-helper/
COPY otel_helper/ /tmp/otel-helper/otel_helper/
RUN pip install --no-cache-dir /tmp/otel-helper

COPY example/python-backend/requirements.txt .
RUN grep -v "otel-helper\|setuptools" requirements.txt | pip install --no-cache-dir -r /dev/stdin

COPY example/protos/ /app/protos/
RUN python -m grpc_tools.protoc -I/app/protos --python_out=/app --grpc_python_out=/app /app/protos/notification.proto

COPY example/python-backend/ /app/

EXPOSE 50051
CMD ["python", "server.py"]
