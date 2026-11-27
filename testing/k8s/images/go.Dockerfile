# syntax=docker/dockerfile:1
# Go e2e smoke image (testing/e2e/go-smoke) for a Kubernetes Job.
# Build context: the repo tree root (testing/k8s/build-images.sh exports HEAD).
# The helper module is built from source through go.mod's replace (../../../go).
FROM golang:1.26 AS build
WORKDIR /src
COPY go/ go/
COPY testing/e2e/go-smoke/go.mod testing/e2e/go-smoke/go.sum testing/e2e/go-smoke/main.go testing/e2e/go-smoke/
WORKDIR /src/testing/e2e/go-smoke
RUN go mod download
# Static binary; -buildvcs=false: the exported tree has no .git.
RUN CGO_ENABLED=0 GOOS=linux go build -buildvcs=false -trimpath -ldflags="-s -w" -o /out/go-smoke .

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/go-smoke /go-smoke
USER 65532:65532
# One run: span e2e-smoke-span, counter e2e.smoke.runs, WARN log e2e-smoke-log.
# Exits non-zero when setup or the shutdown flush fails.
ENTRYPOINT ["/go-smoke"]
