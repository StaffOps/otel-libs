# Build context: the go/ directory (the example's go.mod replaces the helper
# with ../../, so the helper source must be in the context):
#   docker build -t go-backend -f go/example/go-backend/Dockerfile go/
FROM golang:1.26 AS build
WORKDIR /app
# Dependency layers first (helper module + example module) for build caching.
COPY go.mod go.sum ./
COPY example/go-backend/go.mod example/go-backend/go.sum ./example/go-backend/
WORKDIR /app/example/go-backend
RUN go mod download
WORKDIR /app
COPY . .
WORKDIR /app/example/go-backend
RUN CGO_ENABLED=0 go build -trimpath -o /bin/go-backend .

FROM alpine:3.22
RUN apk add --no-cache ca-certificates
COPY --from=build /bin/go-backend /bin/go-backend
USER 65534
ENTRYPOINT ["/bin/go-backend"]
