# Go demo worker (specs/demo-environment DE3a). Build context: the repo root,
# because the demo module replaces the helper with ../ and the backend needs
# demo/sql/schema.sql (DE-D3); go/demo/worker/Dockerfile.dockerignore keeps the
# context to those files:
#   docker build --platform linux/amd64 -f go/demo/worker/Dockerfile -t otel-libs-demo-go-worker:dev .
# The build stage runs on the build host's platform and cross-compiles a
# static linux/amd64 binary (CGO_ENABLED=0); the runtime stage runs non-root.
# Base images are pinned by digest (multi-arch index digests, DE-D7 r3.5).
FROM --platform=$BUILDPLATFORM golang:1.26@sha256:4165ee6bf3b7811fd7b40373290405c83d0167dcb87ee5f29c427cdcef45ff8f AS build
WORKDIR /src/go
# Dependency layers first (helper, extensions, demo module) for build caching.
COPY go/go.mod go/go.sum ./
COPY go/ext/otelsql/go.mod go/ext/otelsql/go.sum ./ext/otelsql/
COPY go/ext/otelredis/go.mod go/ext/otelredis/go.sum ./ext/otelredis/
COPY go/demo/go.mod go/demo/go.sum ./demo/
WORKDIR /src/go/demo
RUN go mod download
WORKDIR /src/go
COPY go/ ./
WORKDIR /src/go/demo
ARG TARGETARCH=amd64
RUN CGO_ENABLED=0 GOOS=linux GOARCH=$TARGETARCH go build -trimpath -buildvcs=false -ldflags="-s -w" -o /out/demo-worker ./worker

FROM alpine:3.22@sha256:49340b346798875b5592e5d420241f2f9cfba5f47e62ebb1446a1b34734e898f
RUN apk add --no-cache ca-certificates
COPY --from=build /out/demo-worker /usr/local/bin/demo-worker
USER 65534:65534
ENTRYPOINT ["/usr/local/bin/demo-worker"]
