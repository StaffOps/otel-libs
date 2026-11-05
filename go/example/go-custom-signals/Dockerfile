# Build context: the go/ directory (the example's go.mod replaces the helper
# with ../../, so the helper source must be in the context):
#   docker build -t go-custom-signals -f go/example/go-custom-signals/Dockerfile go/
FROM golang:1.26 AS build
WORKDIR /app
# Dependency layers first (helper module + example module) for build caching.
COPY go.mod go.sum ./
COPY example/go-custom-signals/go.mod example/go-custom-signals/go.sum ./example/go-custom-signals/
WORKDIR /app/example/go-custom-signals
RUN go mod download
WORKDIR /app
COPY . .
WORKDIR /app/example/go-custom-signals
RUN CGO_ENABLED=0 go build -trimpath -o /bin/go-custom-signals .

FROM alpine:3.22
RUN apk add --no-cache ca-certificates
COPY --from=build /bin/go-custom-signals /bin/go-custom-signals
USER 65534
ENTRYPOINT ["/bin/go-custom-signals"]
