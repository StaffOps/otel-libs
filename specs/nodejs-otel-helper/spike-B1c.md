# Node.js helper — B1c spike gate (go/no-go)

**Status**: Done 2026-10-29 — **GO**. Every gate passes on Node 22.15.0, 22.23.4,
24.22.2 and 26.13.0 (linux/arm64, `node:<v>-slim`). Two findings need spec
changes. **F4 applied 2026-10-29** in design D-JS3 — a technical packaging
correction backed by this spike's `npm ls` evidence, decided by the
implementing session within the owner's Phase B go-ahead (no contract or
behavior change), confirmed in the code-review re-review (2026-10-29). **F5 recorded as a proposed
D-JS14 row; the fixture decision (DNS-named TLS receiver vs a skip on the
Node 26 leg) is taken in B2** because it touches the fakeotlp certificate
shared with Go/Python.
Spec: [requirements](requirements.md) · [design](design.md) · [tasks](tasks.md).

## Setup

| Item | Value |
|---|---|
| Code | `nodejs/spike/` (private, not published; since the B1 review **not a workspace member**: own `package-lock.json`, the packed core installed with `--no-save`, so its deps never hoist into the core tests); helper `/hooks` from `nodejs/packages/otel-helper` (B1a) |
| Stand-in setup | `spike/setup-stub.cjs`, in the D-JS1 order: `registerInstrumentations([http, grpc])` → exporters/credentials → `TracerProvider` + `SimpleSpanProcessor({ exporter })` → `AsyncLocalStorageContextManager().enable()` → W3C propagator → global provider |
| Versions (lockfile) | api 1.10.0; core/resources/sdk-trace/sdk-metrics/sdk-logs/context-async-hooks 3.0.0; instrumentation, instrumentation-http/-grpc, otlp-exporter-base, otlp-grpc-exporter-base, otlp-transformer, exporter-trace-otlp-grpc 0.300.0; import-in-the-middle 3.5.3; require-in-the-middle 8.0.1; @grpc/grpc-js 1.14.7 |
| Receiver | `fakeotlp-node` (compose); TLS listener `127.0.0.2:14317`, cert SANs include DNS `fakeotlp` |
| Images | `node:22.15.0-slim`, `node:22-slim` (22.23.4), `node:24-slim` (24.22.2), `node:26-slim` (26.13.0) |
| Arch | **linux/arm64** (local Docker on Apple silicon). CI (`ubuntu-latest`) is amd64 and does not run the spike; no arch-specific code is involved (pure JS, no native addons), but amd64 is unmeasured |

Command (per version), from the repo root:

```sh
NODE_VERSION=<22.15.0|22|24|26> docker compose -f testing/docker-compose.yml \
  run --rm node-tests sh /src/nodejs/spike/run-in-docker.sh
```

`nodejs/spike/run-in-docker.sh` copies the workspace out of the read-only
mount, `npm ci --ignore-scripts`, builds and packs the core, installs the
spike's lockfile plus the packed core (`--no-save`), maps `fakeotlp` to
`127.0.0.2` in the container's `/etc/hosts` and runs `node run-spike.cjs`.
A gate fails on a wrong result **or on any stderr output** (check 5 tolerates only
DEP0123, see F5). Controls are recorded, never gate.

## Results per Node version

| Check | 22.15.0 | 22.23.4 | 24.22.2 | 26.13.0 |
|---|---|---|---|---|
| Hooks mode chosen by `/hooks` (`supportsSyncHooks()`) | async (`module.register`) | sync | sync | sync |
| 1a ESM `import http from 'node:http'`, custom preload | PASS | PASS | PASS | PASS |
| 1b ESM `import { createServer, get } from 'node:http'` | PASS | PASS | PASS | PASS |
| 1c/1d control: same apps, preload without `/hooks` | only the manual span | same | same | same |
| 2a CJS `--require` preload, `require('node:http')` | PASS | PASS | PASS | PASS |
| 2b CJS, destructured `require` | PASS | PASS | PASS | PASS |
| 2c control: CJS without `/hooks` | HTTP spans present | same | same | same |
| 3a one copy per package, workspace tree | PASS | PASS | PASS | PASS |
| 3b one copy per package, fresh install of the packed core + app `@grpc/grpc-js` | PASS | PASS | PASS | PASS |
| 4 child span parent = HTTP server span; server parent = client span; one trace | PASS (in 1a–2b) | PASS | PASS | PASS |
| 5 explicit `createSslCredentials` → TLS `fakeotlp:14317`, env poisoned after setup | PASS | PASS | PASS | PASS |
| 5 explicit `createInsecureCredentials` → plain `127.0.0.2:4317`, TLS env set after setup | PASS | PASS | PASS | PASS |
| 5 control: explicit TLS to the IP literal `127.0.0.2:14317` | export OK + DEP0123 warning | same | same | **export FAILED** (F5) |
| 5 control: exporter (and grpc-js) loaded **before** `registerInstrumentations` | gRPC server span present | same | same | same |
| 5 control: no explicit credentials, CA env set only after construction | export OK over TLS | same | same | same |
| 5 control: no explicit credentials, CA env valid at construction, bogus after | export FAILED: `14 UNAVAILABLE: … unable to verify the first certificate` | same as 22.15.0 | `14 UNAVAILABLE: … unable to verify the first certificate; if the root CA is installed locally, try running Node.js with --use-system-ca` | same as 24.22.2 |

The last row was re-run after the B1 review (2026-10-29, `run-in-docker.sh`).
A reviewer reported `DEADLINE_EXCEEDED` on 26 for this control; that was not
reproduced here (likely timing: the 5 s `timeoutMillis` against a failing
handshake). Either code is an export failure, which is what the control shows.

Checks 1/2: each pass means `["INTERNAL:child-in-handler","SERVER:GET","CLIENT:GET"]`
were exported and parented as stated. The ESM app gets its tracer from the **ESM**
build of `@opentelemetry/api` while the stub registered the provider through the
**CJS** build. The OTel 3.0 packages ship separate `.cjs`/`.mjs` builds, and the
api global registry links them: proven by the child span landing in the stub's
provider.

Check 3: every listed package resolves to one path in both trees, e.g.
`"@grpc/grpc-js":["1.14.7 node_modules/@grpc/grpc-js"]`, `"import-in-the-middle":["3.5.3 …"]`
(full JSON per version in the run output).

Check 5 gates also assert: the gRPC server span `grpc.spike.Spike/Ping` exists
(the app's grpc-js is patched), grpc-js was **not** loaded when
`registerInstrumentations` ran, the app and the exporter resolve the same grpc-js
file, and the receiver logged the export with the expected `tls`/`port`
(e.g. `{"proto":"grpc","tls":true,"port":"14317","path":"/opentelemetry.proto.collector.trace.v1.TraceService/Export"}`).
After setup, the TLS variant sets `OTEL_EXPORTER_OTLP[_TRACES]_CERTIFICATE=/nonexistent/ca.pem`
and `…_INSECURE=true`, and the export still succeeds over TLS. **The exporter with
explicit credentials reads no TLS env after construction.** The two `lazy-env`
controls show the opposite for an exporter without explicit credentials: it
reads the certificate env at first export (ADR-012's premise, confirmed).

## Findings

**F1 — iitm sync hooks + `--eval`/`--print` crash.** With
import-in-the-middle 3.5.3's sync hooks installed from a `--require` preload,
`node --require <preload> -e 1` (or `-p`) dies with
`Error: ENOENT: no such file or directory, open 'module'` in
`create-hook.mjs:881` (`loadSync`, reached from `loadBuiltinWithHooks`).
Reproduced 2026-10-29 on **every sync-hooks version: 22.23.4, 24.22.2, 26.13.0**
(22.15.0 uses the async loader and is unaffected). It also hits
`NODE_OPTIONS=--require=@staffops/otel-helper/register node -e …`, the usual
shape of a container HEALTHCHECK, so the impact is not limited to one-liners.
File entry points work. A no-op `module.registerHooks({ resolve, load })` from
the same preload does not crash on any of the four versions, so the bug is in
iitm, not Node. `process._eval` is already set while the preload runs (checked
on all four).

**Fix (B1 review, 2026-10-29)**: `installHooks()` never installs iitm sync hooks
under `--eval`/`--print`. Before Node 26 it uses the async `module.register`
loader instead. On Node >= 26, where `module.register` is deprecated (DEP0205),
it installs no ESM hook (`hooks: 'cjs-only'`; `require()` patching is
require-in-the-middle's and still works) and writes one line:
`otelhelper: ESM import hooks not installed under --eval/--print on this Node; only require() is instrumented node=<v>`.
Tests (`hooks.test.cjs`, run on every leg): `-r …/register -e`,
`-r …/hooks -p`, `NODE_OPTIONS=--require=…/register` + `-e`,
`--import …/register -e`, and an ESM `import()` after the async loader under
`-e`.

Upstream issue draft (for the owner to file at nodejs/import-in-the-middle):

> **Title**: Sync hooks (`register-hooks.mjs`) crash `node --eval` / `--print` with `ENOENT … open 'module'`
>
> **Versions**: import-in-the-middle 3.5.3; Node 22.23.4, 24.22.2, 26.13.0 (linux/arm64).
>
> **Repro**: `npm i import-in-the-middle@3.5.3`, then
> `echo "require('import-in-the-middle/register-hooks.mjs').register()" > reg.cjs`, then
> `node --require ./reg.cjs -e 1`. The same happens with `-p 1`, and with
> `NODE_OPTIONS=--require=./reg.cjs node -e 1`.
>
> **Actual**: `Error: ENOENT: no such file or directory, open 'module'`, with
> frames `defaultLoadImpl (node:internal/modules/cjs/loader)` → `loadSync
> (create-hook.mjs:881)` → `loadBuiltinWithHooks` → `Module._load`.
>
> **Expected**: exit 0, as with `node --require ./reg.cjs file.cjs`, `node
> --import <esm register> -e 1`, or a no-op `module.registerHooks({ resolve:
> (s, c, n) => n(s, c), load: (u, c, n) => n(u, c) })` preload (all fine on the
> same versions).
>
> **Notes**: only the eval entry is affected; the failing load is the builtin
> `module`, which the loader treats as a file path.

**F2 — Node runs `--require` preloads in its module-hooks thread too.** A no-op
loader registered with `module.register()` from a `--require` preload has its
`initialize` called **twice** (22.15.0 and 24.22.2 checked). With iitm that
prints `Warning: The 'import-in-the-middle' hook has already been initialized`.
Fixed in B1a: `installHooks()` returns early when `worker_threads.isInternalThread`
(Node >= 22.14) is true. Test: `hooks.test.cjs` "a --require preload registers
the async loader once". No spec change.

**F3 — `module.register()` is runtime-deprecated on Node 26 (DEP0205).** The
async fallback only runs where `supportsSyncHooks()` is false (22.15–22.22.2,
24.0–24.11.0), which predates the deprecation, so production never hits it.
Tests that force the async path tolerate exactly that warning. No change
required. Optional, owner's call: raising `engines` to `>=22.22.3` would drop the
async path, but it contradicts JS1's floor and the upstream CI set.

**F4 — the exact pin on `otlp-grpc-exporter-base` would cause the duplicate
copies D-JS2 avoids.** The D-JS3 table says the base is "pinned exactly: same
grpc-js copy as the exporter". The exporters already depend on it exactly
(`exporter-trace-otlp-grpc@0.300.0` → `"@opentelemetry/otlp-grpc-exporter-base": "0.300.0"`,
npm metadata). An exact pin in the helper next to `~0.300.0` exporters would
produce two copies as soon as a `0.300.1` exporter resolves. The copy of grpc-js
is guaranteed by dedupe instead: both declare `^1.14.5`, and check 3 shows one
copy. B1a uses `~0.300.0` like every other experimental package.
**Proposed spec change**: D-JS3 table, gRPC channel credentials row: "pinned
exactly" → "same `~0.300.x` range as the gRPC exporters (one copy, checked by
`npm ls` in CI)".

**F5 — Node 26 rejects an IP literal as TLS ServerName; grpc-js 1.14.7 sets it.**
Over TLS to `https://127.0.0.2:14317`, Node 22/24 print
`[DEP0123] DeprecationWarning: Setting the TLS ServerName to an IP address is not permitted by RFC 6066`.
Node 26 fails the connection instead:
`14 UNAVAILABLE: … TypeError [ERR_INVALID_ARG_VALUE]: The property 'options.servername' Setting the TLS ServerName to an IP address is not permitted.. Received '127.0.0.2'`.
This happens with and without explicit credentials (the `lazy-env` control
failed the same way on 26 before the DNS-name change), so it is not caused by
the helper. A DNS name (`fakeotlp`, mapped in `/etc/hosts`) works on all four.
Consequences:
- Users with an IP-literal `https://` gRPC endpoint cannot export on Node 26, and
  on 22/24 they get a stderr warning the helper did not write.
- The parity fixture's gRPC TLS cases dial IP literals (`{receiver_tls}` =
  `127.0.0.3:4317`, and `127.0.0.2:14317`). On the Node 26 leg they will fail. On
  22/24 the DEP0123 line can break assertions on diagnostics output.

**Proposed spec changes** (for B2/B4b, not applied):
1. design D-JS14: new row "gRPC over TLS to an IP-literal endpoint fails on
   Node >= 26 (grpc-js sets an IP ServerName; Node 22/24 warn DEP0123)",
   status: recorded gap, plus a helper warning when the resolved gRPC TLS
   endpoint host is an IP literal.
2. D-JS12 / B2: the Node parity runner either maps a DNS name for the TLS
   receivers (e.g. `fakeotlp` / a new SAN for `127.0.0.3`, via `/etc/hosts` in
   `node-tests` and an `--add-host` in the CI action) and rewrites
   `{receiver_tls}` for Node only, or marks the IP-literal TLS cases
   `skip:node26-ip-servername` on 26. The first keeps the cases running and is
   recommended. It changes `testing/fakeotlp` (cert SAN) and the fixture
   placeholder semantics, both shared with Go/Python, so it needs a fixture-case
   decision.
3. B4b: check whether grpc-js's `grpc.ssl_target_name_override` channel option
   (or a newer grpc-js) avoids the IP ServerName. Not tested in this spike.

**F6 — the D-JS1 construction order is not load-bearing for grpc-js 1.14.7 +
instrumentation-grpc 0.300.0 (CJS).** In the `exporters-first` control the
exporter base had already loaded grpc-js (`create-service-client-constructor.cjs`
requires it at module top level) before `registerInstrumentations`. The app's
gRPC server span was still produced on every version. Keep the order: it is
free, and it protects against instrumentations that patch internal files on
load. B6 should not claim the reverse order breaks patching, because this
evidence says it does not today. No spec change required. Optional wording fix
in D-JS1: "required" → "defensive".

**F7 — 3.0 API shape used by later tasks.** `SimpleSpanProcessor` and
`BatchSpanProcessor` take an options object (`{ exporter, … }`), not the
exporter (`sdk-trace` 3.0.0 `types.d.cts:67-71`). Passing the exporter
positionally fails at the first export with
`TypeError: Cannot read properties of undefined (reading 'export')`. Relevant
for B4b. Not in research.md today.

## Go / no-go

**GO.** D-JS1 (explicit context manager, credentials after instrumentation),
D-JS10 (one CJS implementation shared by `require`/`import`, shared api globals
across the OTel dual builds) and D-JS11 (iitm sync hooks, else `module.register`)
hold on all four Node versions. F4 and F5 need the spec changes above before
B2/B4b.
