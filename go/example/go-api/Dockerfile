FROM golang:1.22 AS build
WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -o /bin/go-api .

FROM alpine:3.20
RUN apk add --no-cache ca-certificates
COPY --from=build /bin/go-api /bin/go-api
ENTRYPOINT ["/bin/go-api"]
