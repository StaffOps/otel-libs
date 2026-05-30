FROM python:3.11-slim

WORKDIR /app

# Install otel-helper from local source
COPY pyproject.toml /tmp/otel-helper/
COPY otel_helper/ /tmp/otel-helper/otel_helper/
RUN pip install --no-cache-dir /tmp/otel-helper

# Install app dependencies
COPY example/python-api/requirements.txt .
RUN grep -v "otel-helper\|setuptools" requirements.txt | pip install --no-cache-dir -r /dev/stdin

# Generate gRPC stubs
COPY example/protos/ /app/protos/
RUN python -m grpc_tools.protoc -I/app/protos --python_out=/app --grpc_python_out=/app /app/protos/notification.proto

COPY example/python-api/ /app/

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
