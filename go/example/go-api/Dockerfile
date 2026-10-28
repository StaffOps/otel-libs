# Build context: the go/ directory (the example's go.mod replaces the helper
# with ../../, so the helper source must be in the context):
#   docker build -t go-api -f go/example/go-api/Dockerfile go/
FROM golang:1.26 AS build
WORKDIR /app
# Dependency layers first (helper module + example module) for build caching.
COPY go.mod go.sum ./
COPY example/go-api/go.mod example/go-api/go.sum ./example/go-api/
WORKDIR /app/example/go-api
RUN go mod download
WORKDIR /app
COPY . .
WORKDIR /app/example/go-api
RUN CGO_ENABLED=0 go build -trimpath -o /bin/go-api .

FROM alpine:3.22
RUN apk add --no-cache ca-certificates
COPY --from=build /bin/go-api /bin/go-api
USER 65534
ENTRYPOINT ["/bin/go-api"]
