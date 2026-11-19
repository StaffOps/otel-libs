# Build context: the go/ directory (the example's go.mod replaces the helper
# with ../../, so the helper source must be in the context):
#   docker build -t go-process -f go/example/go-process/Dockerfile go/
FROM golang:1.26 AS build
WORKDIR /app
# Dependency layers first (helper module + example module) for build caching.
COPY go.mod go.sum ./
COPY example/go-process/go.mod example/go-process/go.sum ./example/go-process/
WORKDIR /app/example/go-process
RUN go mod download
WORKDIR /app
COPY . .
WORKDIR /app/example/go-process
RUN CGO_ENABLED=0 go build -trimpath -o /bin/go-process .

FROM alpine:3.22
RUN apk add --no-cache ca-certificates
COPY --from=build /bin/go-process /bin/go-process
USER 65534
ENTRYPOINT ["/bin/go-process"]
