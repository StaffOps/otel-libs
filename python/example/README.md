# Python Examples — Notification Service

Notification service demonstrating the `otel-helper` lib in a real scenario with 3 components.

## Architecture

```
python-process (Worker)
  ├── QueueConsumer (30s) → consumes queue, calls API
  ├── RetryWorker (60s)   → reprocesses failures
  ├── CleanupWorker (3m)  → cleans expired
  └── HealthChecker (1m)  → checks API
         │
         │ HTTP :8000
         ▼
python-api (FastAPI)
  ├── POST /notify        → sends notification
  ├── GET /notify/{id}    → checks status
  ├── GET /templates      → lists templates
  ├── POST /templates     → creates template
  └── GET /metrics/summary
         │
         │ gRPC :50051
         ▼
python-backend (gRPC Server)
  ├── SendNotification    → renders + delivers (email/sms/push)
  ├── GetStatus           → checks delivery status
  └── RenderTemplate      → renders template with variables
```

## Build

```bash
cd python/example

docker build -t python-api -f python-api/Dockerfile .
docker build -t python-backend -f python-backend/Dockerfile .
docker build -t python-process -f python-process/Dockerfile .
```

## Run local

```bash
docker network create notify-net

docker run -d --name python-backend --network notify-net \
  -e SERVICE_NAME=python-backend \
  -e ENVIRONMENT=LOCAL \
  python-backend

docker run -d --name python-api --network notify-net -p 8000:8000 \
  -e SERVICE_NAME=python-api \
  -e ENVIRONMENT=LOCAL \
  -e BACKEND_URL=python-backend:50051 \
  python-api

docker run -d --name python-process --network notify-net \
  -e SERVICE_NAME=python-process \
  -e ENVIRONMENT=LOCAL \
  python-process
```

## Test

```bash
# Send notification
curl -X POST http://localhost:8000/notify \
  -H "Content-Type: application/json" \
  -d '{"recipient": "user@example.com", "channel": "email", "template_id": "welcome", "variables": {"name": "User", "product": "MyApp"}}'

# Check status
curl http://localhost:8000/notify/{notification_id}

# List templates
curl http://localhost:8000/templates
```

## Generated traces

Each `POST /notify` generates a distributed trace:

```
api.send-notification (root)
└── gRPC NotificationService/SendNotification
    ├── backend.render-template
    └── backend.deliver
```

The worker generates independent traces per cycle:

```
process.queue-consume (root)
├── process.dispatch-notification → HTTP POST /notify → (trace above)
├── process.dispatch-notification → ...
└── ...
```
