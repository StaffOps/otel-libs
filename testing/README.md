# Test infrastructure

| Path | What |
|---|---|
| `fakeotlp/` | Fake OTLP receiver for the parity fixture's `export` cases. Own Go module (not part of `go/`). Every listen address accepts OTLP/gRPC and OTLP/HTTP; `PLAIN`, `TLS` and `ADMIN` set the addresses (TLS uses a CA generated at startup, `$OUT/ca.pem`); `GET/DELETE <admin>/requests`. Defaults to the case addresses below. Records destination IP, port, protocol, TLS, path, content/`grpc-encoding`, `grpc-timeout` and `x-parity*` headers — never payloads. `CERTS_ONLY=1` writes a CA + server cert/key for `TLS_HOSTS` and exits (used by the e2e Collector). Runs as uid 65534 (`cap_net_bind_service` for 80/443). Test-only: no auth. |
| `docker-compose.yml` | `go-tests` / `python-tests` / `node-tests`, each sharing the network namespace of its own receiver (`fakeotlp-go` / `fakeotlp-py` / `fakeotlp-node`), and the `e2e` profile (`e2e-certs`, `collector`, `go-smoke`, `python-smoke`). |
| `e2e/` | `smoke.sh` + `combos.sh` + `run-go.sh` / `run-python.sh`: a Go smoke program and the Python `python-process` example export to `otel/opentelemetry-collector-contrib` over gRPC, http/protobuf with an `/otlp` path prefix, a per-signal traces endpoint, and TLS with a generated CA. Each receiver tags its data (`e2e.receiver`); the script checks every signal arrived at the right receiver in a batch with the combination's `service.name`, reports known bugs as `XFAIL <task>`, and checks the per-signal traces did not also reach the default receiver. |

```bash
docker compose -f testing/docker-compose.yml run --rm go-tests
PY_VERSION=3.10 PY_DEPS=lower docker compose -f testing/docker-compose.yml run --rm python-tests   # defaults: 3.13 / latest
testing/e2e/smoke.sh
docker compose -f testing/docker-compose.yml --profile e2e stop   # stop the receivers / Collector when done
```

## Receiver addresses

The receiver binds only the case addresses, so other unit tests exporting to
`localhost:4317` get connection-refused instead of polluting its request log:

| Address | Listeners |
|---|---|
| `127.0.0.2` | plain `4317 4318 80 24318`, TLS `14317 14318 443`, admin `8888` |
| `127.0.0.3` | TLS `4317` (`{receiver_tls}`: the D1.b default port with TLS) |
| `[::1]` | plain `24318` (IPv6 cases) |

The certificate also carries the DNS names `fakeotlp` (127.0.0.2) and
`fakeotlp-tls` (127.0.0.3). The Node.js legs use them instead of the IPs
(`FAKEOTLP_HOST=fakeotlp`, `FAKEOTLP_HOST_TLS=fakeotlp-tls`): Node 26 refuses
an IP literal as TLS ServerName (design D-JS14). Compose maps them with
`extra_hosts` on `fakeotlp-node` (`node-tests` shares its `/etc/hosts`); CI
appends them to the runner's `/etc/hosts` in the `node-test` job.

The SANs are compiled into the receiver, so an `otel-libs/fakeotlp:local`
image built before them makes the Node TLS checks fail. Rebuild it before
running `node-tests` (`testing/verify.sh --only node` does):
`docker compose -f testing/docker-compose.yml build fakeotlp-node`, then
`docker compose -f testing/docker-compose.yml up -d --force-recreate fakeotlp-node`
if a receiver is already running.

Runner environment (set by compose and by `.github/actions/fakeotlp` in CI):

| Variable | Value |
|---|---|
| `FAKEOTLP_HOST` / `FAKEOTLP_HOST_TLS` / `FAKEOTLP_HOST_IPV6` | `127.0.0.2` / `127.0.0.3` / `[::1]`; Node legs: `fakeotlp` / `fakeotlp-tls` / `[::1]` (DNS names, below) |
| `FAKEOTLP_ADMIN` | `http://127.0.0.2:8888` |
| `FAKEOTLP_CA` | path to the receiver's `ca.pem` |
| `PARITY_REQUIRE_FAKEOTLP` | `1`: receiver-dependent cases fail instead of skipping |

Export cases share the receiver's request log, so each suite runs them one at
a time. Go and Python have separate receivers and can run concurrently; two
runs of the same suite at once need separate compose projects (`-p <name>`).

## CI

`.github/workflows/ci.yml` runs the receiver on the runner's loopback
(`--network host`) through the composite action, the Go suite (merged parity
coverage, ≥ 90% gate), the Python grid (3.10 / 3.11 / 3.13 × `lower` /
`latest` constraints from `python/ci/`), and the e2e smoke as its own job.
