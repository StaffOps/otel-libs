# Go Examples — Anomaly Detection Service

Anomaly detection service demonstrating the `otelhelper` lib in a real scenario with 3 components.

## Architecture

```
go-process (Background Worker)
  ├── anomalyScanner (30s)   → scans metrics, detects anomalies
  ├── alertDispatcher (60s)  → dispatches pending alerts
  └── cleanupWorker (3m)     → cleans expired detections
         │
         │ HTTP :8080
         ▼
go-api (HTTP API)
  ├── POST /detect           → submits detection (calls backend)
  ├── GET /status/{id}       → checks detection status
  ├── GET /metrics/summary   → business metrics
  └── GET /health            → health check (no tracing)
         │
         │ HTTP :50051
         ▼
go-backend (Detection Engine)
  ├── POST /detect           → anomaly detection (50-500ms latency)
  ├── GET /status/{id}       → returns detection status
  └── GET /health            → health check
```

## Build

```bash
cd go/example

docker build -t go-api -f go-api/Dockerfile go-api/
docker build -t go-backend -f go-backend/Dockerfile go-backend/
docker build -t go-process -f go-process/Dockerfile go-process/
```

## Run local

```bash
docker network create anomaly-net

docker run -d --name go-backend --network anomaly-net \
  -e SERVICE_NAME=go-backend \
  -e ENVIRONMENT=LOCAL \
  go-backend

docker run -d --name go-api --network anomaly-net -p 8080:8080 \
  -e SERVICE_NAME=go-api \
  -e ENVIRONMENT=LOCAL \
  -e BACKEND_URL=http://go-backend:50051 \
  go-api

docker run -d --name go-process --network anomaly-net \
  -e SERVICE_NAME=go-process \
  -e ENVIRONMENT=LOCAL \
  go-process
```

## Test

```bash
# Submit anomaly detection
curl -X POST http://localhost:8080/detect \
  -H "Content-Type: application/json" \
  -d '{"source": "api", "ts": 1717027200}'

# Check detection status
curl http://localhost:8080/status/det-1

# Business metrics
curl http://localhost:8080/metrics/summary

# Health check
curl http://localhost:8080/health
```

## Generated traces

Each `POST /detect` generates a distributed trace:

```
POST /detect (root)
└── detect.submit
    └── POST go-backend:50051/detect
        └── backend.detect
```

Each `GET /status/{id}` propagates context:

```
GET /status/{id} (root)
└── detect.status
    └── GET go-backend:50051/status/{id}
        └── backend.get_status
```

The worker generates independent traces per cycle:

```
process.anomaly_scan (root)    — every 30s
process.alert_dispatch (root)  — every 60s
process.cleanup (root)         — every 3m
```

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SERVICE_NAME` | `my-service` | Service name for resource attributes |
| `ENVIRONMENT` | `LOCAL` | Environment: LOCAL, DEV, HML, PRD |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost` | Collector endpoint |
| `OTEL_HELPER_DEBUG_LEVEL` | `false` | Debug mode |
| `OTEL_HELPER_SAMPLE_RATIO` | `1.0` | Head sampling ratio (0.0-1.0) |
| `BACKEND_URL` | `http://localhost:50051` | Backend URL (go-api only) |

## File structure

```
go/example/
├── go-api/
│   ├── main.go              # HTTP API with context propagation
│   ├── Dockerfile           # Production build
│   ├── Dockerfile.demo      # Demo build (local lib mount)
│   └── go.mod
├── go-backend/
│   ├── main.go              # Detection engine (simulated)
│   ├── Dockerfile
│   ├── Dockerfile.demo
│   └── go.mod
├── go-process/
│   ├── main.go              # Background workers with StartRootSpan
│   ├── Dockerfile
│   ├── Dockerfile.demo
│   └── go.mod
├── protos/
│   └── anomaly.proto        # Reference proto (not compiled)
└── README.md
```
