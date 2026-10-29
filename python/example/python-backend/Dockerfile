FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml /tmp/staffops-otel-helper/
COPY staffops_otel_helper/ /tmp/staffops-otel-helper/staffops_otel_helper/
COPY otel_helper/ /tmp/staffops-otel-helper/otel_helper/
RUN pip install --no-cache-dir /tmp/staffops-otel-helper

COPY example/python-backend/requirements.txt .
RUN grep -v "staffops-otel-helper\|setuptools" requirements.txt | pip install --no-cache-dir -r /dev/stdin

COPY example/protos/ /app/protos/
RUN python -m grpc_tools.protoc -I/app/protos --python_out=/app --grpc_python_out=/app /app/protos/notification.proto

COPY example/python-backend/ /app/

EXPOSE 50051
CMD ["python", "server.py"]
