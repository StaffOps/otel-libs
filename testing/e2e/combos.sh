# Shared e2e combinations: "<name>|<env assignments>" (sourced by run-*.sh and smoke.sh).
COMBOS='grpc|OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4317
http-prefix|OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4320/otlp OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
per-signal|OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4317 OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://collector:4319
tls|OTEL_EXPORTER_OTLP_ENDPOINT=https://collector:14317 OTEL_EXPORTER_OTLP_CERTIFICATE=/certs/ca.pem'
