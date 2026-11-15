# Kubernetes test tooling (`testing/k8s/`)

Runs the helpers on a real cluster and checks what reached the backends. The
Compose e2e (`testing/e2e/`) proves the wire format against a Collector on one
machine; this directory adds what only Kubernetes shows: the node-local OTel
agent, `k8sattributes` enrichment, pod identity and the service graph (built by
the gateway Collector's `service_graph` connector). It is a first, partial
run of the deferred soak Phase K check K2
([`specs/soak-simulation/tasks.md`](../../specs/soak-simulation/tasks.md)).

| File | What it does |
|---|---|
| `build-images.sh` | Builds and pushes the four smoke images (`otel-libs-smoke-<lang>`) |
| `build-examples.sh` | Builds and pushes the twelve example images (`otel-libs-example-<lang>-<app>`) |
| `smoke-jobs.sh` | One Job per language x {grpc, http}; `apply [--verify]`, `delete`, `render` |
| `examples/examples.sh` | The example apps as Deployments; `apply`, `delete`, `render` |
| `verify.py` | Read-only checks in Tempo, Loki and Prometheus; `smoke --run <id>`, `examples` |
| `images/<lang>.Dockerfile` | Smoke images; `images/python-smoke.py` is the Python smoke program |

## Prerequisites

- A kube context (default `k8s01`) that can read the test namespace and use the
  service proxy of the backends: `kubectl get --raw
  /api/v1/namespaces/monitoring/services/<svc>:<port>/proxy/...`.
- The test namespace (default `dev-otel-libs`). Nothing here creates it.
- An OTel agent reachable at
  `otel-agent-collector.opentelemetry.svc.cluster.local:4317` (gRPC) and `:4318`
  (HTTP), with `k8sattributes` and pipelines to Tempo, Loki and Prometheus.
- Backends in namespace `monitoring`: `tempo:3200`, `loki:3100`,
  `kps-prometheus:9090`. The service-graph series
  (`traces_service_graph_request_total`, labels `client`, `server`, `failed`,
  `connection_type`, `client_k8s_cluster_name`) come from the gateway
  Collector's `service_graph` connector, written to Prometheus; Tempo runs no
  `metrics_generator`. `verify.py` takes
  `BACKENDS_NAMESPACE`, `TEMPO`, `LOKI` and `PROMETHEUS` to point elsewhere.
- Docker with a buildx builder (default name `multiarch`:
  `docker buildx create --name multiarch`) and a login to the registry for the
  push: `docker login harbor.staffops.space`. The cluster pulls anonymously, so
  the registry project must allow anonymous pull; no pull secret is used.
- `python3` on the host only to run `verify.py` (standard library; it shells out
  to `kubectl`). Everything else runs in Docker.

## Build the images

Both build scripts export the **committed HEAD** with `git archive`: uncommitted
work never lands in an image. Commit first, or the image shows the previous
commit. The one exception is `testing/k8s/images/` itself, which
`build-images.sh` copies from the working tree.

```bash
testing/k8s/build-images.sh              # go python node dotnet
testing/k8s/build-images.sh python       # one language
testing/k8s/build-examples.sh            # all twelve example apps
testing/k8s/build-examples.sh go python/api
```

Images are `linux/amd64` only (the test node), tagged with the short HEAD sha
and `latest`: `harbor.staffops.space/development/otel-libs-smoke-<lang>` and
`.../otel-libs-example-<lang>-<app>`. `TAG`, `REGISTRY` and `BUILDER` override
the defaults. `build-examples.sh` stops when the local Docker daemon has less
than `MIN_FREE_GB` (3) free: the .NET and Node.js builds are large. The probe
measures the local daemon's disk, not a remote or `docker-container` buildx
builder's storage.

`smoke-jobs.sh` and `examples.sh` default `TAG` to the short HEAD sha too, so
they run the images built from the current commit; after a new commit, build
again or pass `TAG=latest` (or an older sha). Both print the tag they applied.

## Smoke Jobs

Each Job runs one smoke program once: span `e2e-smoke-span`, counter
`e2e.smoke.runs`, WARN log `e2e-smoke-log` inside the span. Service
`e2e-<lang>-k8s-<combo>`, configured only with standard OTel variables
(`OTEL_EXPORTER_OTLP_ENDPOINT`, `_PROTOCOL`, `OTEL_SERVICE_NAME`,
`OTEL_RESOURCE_ATTRIBUTES` with `otel_libs.run=<run>`,
`OTEL_METRIC_EXPORT_INTERVAL=1000`).

```bash
RUN=r2 testing/k8s/smoke-jobs.sh apply              # 8 Jobs, prints run=r2
RUN=r2 testing/k8s/smoke-jobs.sh apply --verify     # also waits and verifies (or VERIFY=1)
LANGS="go python" RUN=r3 testing/k8s/smoke-jobs.sh apply
testing/k8s/smoke-jobs.sh render                    # print the manifests only
testing/k8s/smoke-jobs.sh delete                    # every e2e-smoke Job
```

`--verify` polls the run's Jobs every 5 s until all succeeded, one failed
(it stops there) or `WAIT_TIMEOUT` seconds (300) passed, sleeps `VERIFY_DELAY`
(30 s) for ingestion, runs `verify.py smoke --run $RUN` over the last hour and
exits non-zero when a Job did not succeed or a check failed. `LANGS` and `RUN`
(lowercase letters, digits, `-`) are validated before anything is applied.
Without it `apply` behaves as before. Jobs clean themselves up 24 h after they
finish (`ttlSecondsAfterFinished`).

Every smoke program exits 1 when its telemetry did not leave. The Python SDK
reports a refused or unreachable export only in its log —
`shutdown_telemetry()` and the providers' `force_flush()` still succeed
(measured, grpc and http/protobuf, against a closed port) — so
`python-smoke.py` counts ERROR records on the `opentelemetry.exporter.otlp`
loggers and exits 1 when there is one. Retries of transient errors log at
WARNING and do not count. Measured 2026-11-15: exit 0 against a Collector,
exit 1 against `http://127.0.0.1:9` for both protocols. This detection is
verified by hand, not in CI: `testing/k8s/images/check-python-refused.sh`
builds the image locally (or runs `IMAGE=...`) and asserts exit 1 against a
closed port for both protocols. It depends on the SDK's exporter logger names;
if upstream renames them, a refused export exits 0 again. The image in the
registry on 2026-11-15 predates this check: rebuild it (`build-images.sh
python`) to get it.

## Example apps

```bash
testing/k8s/examples/examples.sh apply                    # compact layout, all four languages
LANGS="go python" testing/k8s/examples/examples.sh apply  # a subset
LAYOUT=split testing/k8s/examples/examples.sh apply       # one pod per app
testing/k8s/examples/examples.sh render
LANGS=dotnet testing/k8s/examples/examples.sh delete      # by the otel-libs/lang label
```

Wiring comes from the examples' own code: Python, Node.js and .NET run
`process --HTTP--> api --gRPC--> backend`, the process driving traffic. Go runs
`api --HTTP--> backend` and a standalone process; the Go process has no client,
so a `curl` container calls `go-api` every 15 s (`/health` included, which must
produce no span).

### The pod limit and the compact layout

The test node runs at its 110-pod limit with about two pods free, so twelve app
pods do not fit. `LAYOUT=compact` (the default) puts each language's apps in one
Deployment as containers of one pod, wired over `127.0.0.1`: five pods
(`go-stack`, `go-process` — it listens on `:8080` like `go-api` — `python-stack`,
`node-stack`, `dotnet-stack`). Even five may not fit next to the smoke Jobs.
Rotate languages: delete one, apply the next.

```bash
LANGS="go python" testing/k8s/examples/examples.sh delete
LANGS="node dotnet" testing/k8s/examples/examples.sh apply
```

`verify.py examples` checks current traffic only (the last `--fresh` minutes),
so run it for the languages deployed right now, once per rotation:
`--langs node,dotnet`, then `--langs go,python` after rotating. A language that
is not running fails.

## Verify

```bash
python3 -I testing/k8s/verify.py smoke --run r2
python3 -I testing/k8s/verify.py --langs node,dotnet examples
python3 -I testing/k8s/verify.py --langs go,python --fresh 15 examples
```

Options before the mode: `--context` (`KUBE_CONTEXT`, `k8s01`), `--namespace`
(`NAMESPACE`, `dev-otel-libs`), `--since` (smoke look-back window, `6h`),
`--fresh` (examples: minutes of current traffic, `10`), `--langs`.
Every query goes through the API server's service proxy: read-only, no
port-forward. One `PASS`/`FAIL` line per check, a summary, exit 1 on any FAIL.

`smoke --run <id>`, per service `e2e-<lang>-k8s-<combo>` (four checks):

| Check | Passes when |
|---|---|
| trace | Tempo has a trace of that run rooted at `e2e-smoke-span` |
| k8s resource | its resource has `k8s.namespace.name` = the namespace and a `k8s.pod.name` |
| log | Loki has the `e2e-smoke-log` line of that run, on the same trace_id (Tempo drops leading zeros from trace ids, the comparison ignores them) |
| metric | Prometheus has `e2e_smoke_runs_total` for the service from that run's pod (`smoke-<lang>-<combo>-<run>-*`; `otel_libs.run` is not a metric label) |

`examples`, per language, over the last `--fresh` minutes:

| Check | Passes when |
|---|---|
| edge process -> api, api -> backend | `increase(traces_service_graph_request_total{failed="false"}[<fresh>m]) > 0` for the edge, instant query (Go: api -> backend only) |
| log -> trace | one sampled log line of the language's examples, newer than `--fresh`, has a trace_id Tempo resolves |

Lines whose trace flags have the sampled bit clear are skipped: they carry the
trace_id of a span that was never recorded (see below).

## Measured results (2026-11-15)

Cluster `k8s01`, namespace `dev-otel-libs`, images built from this branch.

- **Smoke**: 8/8 Jobs completed (Go, Python, Node.js, .NET x grpc,
  http/protobuf). The manual check before `verify.py` existed found trace, log
  on the same trace_id and metric for all 8 services (24/24); `verify.py smoke
  --run r1`, which adds the k8s-resource check, passed 32/32.
- **Examples**: the window-based first version of `verify.py examples` passed
  11/11 (each language while or after it ran) — Go api -> backend; Python,
  Node.js and .NET process -> api and api -> backend; for every language a log
  line whose trace resolves in Tempo. The current-traffic version, with Node.js
  and .NET deployed: `--langs node,dotnet` 6/6 (81 and 81 requests on the Node.js
  edges, 39 and 64 on the .NET edges, in 10 minutes); Go and Python, rotated
  out, fail as expected.
- **k8s enrichment**: all 8 smoke resources had `k8s.namespace.name` and
  `k8s.pod.name` (`verify.py`); the resources inspected by hand (one smoke
  trace, one trace per example language, all eleven example services) also had
  `k8s.pod.uid`, `k8s.node.name`, `k8s.cluster.name` and `service.namespace`,
  added by the agent. The apps set only `service.name` and
  `deployment.environment.name` (AGENTS.md principle 4).

Differences found (behavior of the examples or the platform, not checks that
failed):

| Finding | Detail |
|---|---|
| Go examples have no gRPC hop | `go-api` calls `go-backend` over HTTP; the Go process has no client, so `curl` drives `go-api` |
| Go api/backend send no OTel logs | They log with the standard library; only `example-go-process` had log lines with a trace_id in Loki |
| Go client span name `HTTP <method>` | e.g. `HTTP GET`; the other languages name client spans by the method alone (`GET`, `POST`) |
| Python framework spans | The ASGI instrumentation adds `POST /notify http receive` / `http send` internal spans under each server span |
| Node.js framework spans | instrumentation-express adds `request handler - /notify` under each server span |
| Python `/health` access log carries a trace_id | uvicorn's access line for a probe has the trace_id of the filtered health span, trace flags `2` (not sampled): the trace is not in Tempo by design |
| .NET backend -> `unknown` node | `dotnet-backend` calls `httpbin.org` (external enrichment); the service graph shows the edge to `unknown` (`connection_type="virtual_node"`) |
| `k8s.container.name` missing in the compact layout | Pods with three app containers get no `k8s.container.name` (the single-container smoke pods get `smoke`) |
| `service.version` comes from the platform | Smoke resources got `service.version=latest`, the image tag, added by the agent; the helpers do not set it. The multi-container example pods got none |

## Clean up

```bash
testing/k8s/smoke-jobs.sh delete
testing/k8s/examples/examples.sh delete   # all languages; LANGS=... for a subset
```

Both delete only objects in the test namespace labelled with their component
(`app.kubernetes.io/component=e2e-smoke`, or `=example` plus the
`otel-libs/lang` label). Images stay in the registry.
