# Test-only OTLP receiver for the parity fixture (see testing/README.md).
FROM golang:1.26-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY main.go .
RUN CGO_ENABLED=0 go build -trimpath -o /fakeotlp .

FROM alpine:3.22
COPY --from=build /fakeotlp /fakeotlp
# Bind 80/443 as nobody (65534) without a sysctl: file capability. Works with
# Docker's default capability set, including --network host in CI.
RUN apk add --no-cache libcap-setcap \
    && setcap cap_net_bind_service=+ep /fakeotlp \
    && apk del libcap-setcap \
    && mkdir /out && chown 65534 /out
ENV OUT=/out
USER 65534
ENTRYPOINT ["/fakeotlp"]
