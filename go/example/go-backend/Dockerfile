FROM golang:1.22 AS build
WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -o /bin/go-backend .

FROM alpine:3.20
RUN apk add --no-cache ca-certificates
COPY --from=build /bin/go-backend /bin/go-backend
ENTRYPOINT ["/bin/go-backend"]
