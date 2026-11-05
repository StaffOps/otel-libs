---
name: release
description: Use when cutting a release or release candidate of otel-libs (Go, Python, .NET, Node.js) — version bumps, per-language tags, GitHub Release, npmjs/GitHub Packages publish, the Node.js first-publish bootstrap, the Node.js and .NET SDK-bump checklists, verification before and after tagging. Encodes the pitfalls found cutting 0.3.0-rc.1/rc.2.
---

# Release procedure (otel-libs)

Decision record: `specs/config-contract-v0.3/design.md` D10 (+ "A11 ruling").

## Tag scheme (per language — versions diverge, ADR-004)
| Language | Tag | Published by |
|---|---|---|
| Python | `vX.Y.Z[-rc.N]` | CI `publish-python-release` → GitHub Release (wheel + sdist) |
| Go core | `go/vX.Y.Z[-rc.N]` | nothing (module proxy reads the tag) |
| Go ext | `go/ext/{otelaws,otelredis,otelsql}/vX.Y.Z[-rc.N]` | same |
| .NET | `dotnet/vX.Y.Z[-rc.N]` | CI `publish-dotnet-release` → GitHub NuGet: the five `StaffOps.OtelHelper*` packages (from `0.3.0-rc.1`, task C2; `0.2.x` stays published as `OtelHelper*`, no compatibility package, O2) |
| Node.js | `nodejs/vX.Y.Z[-rc.N]` | CI `publish-nodejs-npmjs` → npmjs (trusted publishing, provenance), then `publish-nodejs-ghp` → GitHub Packages; the six packages at one version. **The first release needs the one-time bootstrap below** |

A `v*` tag never publishes .NET or Node.js (`*` does not match `/`). Every
release job fails when the tag does not match the version the source
declares: Python compares the tag with `pyproject.toml` after PEP 440
normalization (`v0.3.0-rc.3` = `0.3.0rc3`), .NET with every project
`<Version>`, Node.js with every location `ci/version.mjs` checks.

## Before tagging
1. All tasks for the release done and validated (change-loop); CHANGELOG
   section states the version and date; migration guide current.
2. Versions: `python/pyproject.toml` in **PEP 440** (`0.3.0rc2`, not
   `0.3.0-rc.2`); Go needs no file change; .NET: the `<Version>` of the five packed
   projects (`dotnet/StaffOps.OtelHelper*/*.csproj` except `.Tests`; the CI
   loops list them by name) equals the tag version; the release job packs them
   with `/p:ContinuousIntegrationBuild=true` (SourceLink, normalized paths —
   `dotnet/Directory.Build.props`). Before the tag, pack locally in Docker with
   `.git` present and check five `StaffOps.OtelHelper*.nupkg`, none named
   `OtelHelper*` (CI `build-lib` and `publish-dotnet` assert it; `publish-dotnet-release` also checks each file name carries the tag version before the push).
   Node.js (SemVer, not PEP 440), in Docker from the repository root:
   ```bash
   docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD/nodejs:/w" -w /w node:24-slim node ci/version.mjs set 0.3.0-rc.1
   docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD/nodejs:/w:ro" -w /w node:24-slim node ci/version.mjs check nodejs/v0.3.0-rc.1
   ```
   `set` moves every location together — the six `packages/*/package.json`
   `version`s, the opt-in packages' exact `@staffops/otel-helper` peer, the
   `VERSION` constants (`otel-helper/src/version.ts`, the
   `-sql`/`-redis`/`-aws` `src/internal/*-instrumentation.ts`), the
   `packages/*` entries of `package-lock.json`, the `ARG HELPER_VERSION`
   default of every `example/*/Dockerfile.demo`, and the mentions of the
   previous version in `nodejs/README.md`, `nodejs/HOW-TO.md` and the package
   READMEs ("Not released yet (`<v>`)", "Release candidate `<v>`", tarball
   names, `@staffops/otel-helper[-x]@<v>`, `` `nodejs/v<v>` ``). `check`
   fails on any mismatch and warns (`WARN … (by hand)`) about README
   mentions it cannot rewrite, and about "release candidate" wording left
   on a stable version; CI runs the same check against the tag.
   (`--user` keeps the files owned by you; `HOME=/tmp` gives npm a writable home.)
   `npm pack` must keep each package's `README.md` (`node ci/check-pack.mjs`).
   Before an SDK or API bump, walk the **Node.js SDK-bump checklist** below.
3. Docs/consumption pages (`CONSUMING.md`, README matrix) name the new tags.
   **By hand for Node.js** — `ci/version.mjs` scans only `nodejs/README.md`,
   `nodejs/HOW-TO.md` and the package READMEs: the repository-root
   `README.md` (matrix row, gaps table, Installing), `CONSUMING.md` (intro,
   Versions row, Node.js section install commands), `AGENTS.md` (Repository)
   and the `nodejs/example/README.md` wording; on a stable release also every
   "release candidate" / dist-tag `next` sentence (`check` warns only about
   "release candidate" in the scanned files). Then regenerate the site index from the README:
   `docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/repo" -w /repo python:3.12-slim python tools/regen_index.py`
   (CI's `spec-checks` runs it with `--check`).
4. `testing/verify.sh --release` green — it covers the CI risk corners that
   local runs used to skip (Python 3.13 + lower deps broke rc.1), and
   `--release` sets `PARITY_RELEASE=1`, so the parity release gate runs: Go
   `TestParityNoTaskSkipsAtRelease` and Python `test_no_task_skips_at_release`
   fail while a case is still `skip:A…`, the Node runner's `release gate`
   test while a `nodejs` status is still `skip:B…`. CI sets the same
   variable on release tags (`go-test`/`python-test` on `v*`, `node-test` on
   `nodejs/v*`), so a pending skip fails the tag's run before any publish job.
   **`--release` is not scoped to one language** (2026-11-04): it exports
   `PARITY_RELEASE=1` for every step the run executes, so a plain
   `--release` before a Node.js tag also fails on a Go/Python case still
   `skip:A…` (and before a `v*` tag on a Node case still `skip:B…`), which
   that tag's CI would not check. Scope it with `--only`: before a
   `nodejs/v*` tag `testing/verify.sh --release --only node` (plus the
   non-gating steps you want, e.g. `--only spec-lint --only trace`); before a
   `v*` tag `--release --only go --only go-ext --only py-3.10-lower --only
   py-3.13-lower --only py-3.13-latest`, then a plain run for the rest.
5. Commit, push the branch, open/update the PR to `main` and wait for its CI
   (the CI push trigger covers only `main` and tags — a pushed feature branch
   alone runs nothing).

## Node.js publish (CI `publish-nodejs-npmjs`, `publish-nodejs-ghp`)
- Trigger: a `nodejs/vX.Y.Z[-rc.N]` tag. `publish-nodejs-npmjs` needs
  `node-test`, `node-api-range`, both `e2e-smoke-node` legs (Node 24 and
  22.15.0) and `build-lib-node`; `publish-nodejs-ghp` needs
  `publish-nodejs-npmjs`. The two jobs never share a token: npmjs gets
  `id-token: write` only, GitHub Packages `packages: write` only.
- `build-lib-node` packs the six tarballs once (on every PR and push too),
  checks them, records their `sha256sum` as a job output and runs `npm
  publish --dry-run` for every version not yet on npmjs.
- Each publish job: `sha256sum --strict -c` of the recorded sums and
  `ci/version.mjs verify-sums` (exactly the six tarballs, no other `.tgz`) →
  `ci/version.mjs check <tag> --tarballs` (name, version and core peer read
  inside each tarball) → publish, core first, then `-sql`, `-redis`, `-aws`,
  `-pino`, `-winston`:
  - npmjs: `npm install -g npm@^11.5.1`, a guard that npm ≥ 11.5.1, then
    `npm publish ./tarballs/<t> --provenance --access public --tag
    <latest|next>` (OIDC, no token);
  - GitHub Packages: the same tarballs, `--@staffops:registry=https://npm.pkg.github.com`
    with `GITHUB_TOKEN` from the environment, no provenance.
- A pre-release (`-rc.N`) goes to dist-tag `next`, never `latest` (npm
  refuses a pre-release without `--tag`).
- Before publishing, `ci/version.mjs registry-state` reads the registry's
  `dist.integrity` for that version: `absent` → publish; `same` (this
  tarball's sha512) → skip, so **re-running the failed job** finishes a
  partial publish; `different` → the job fails (the version was published
  with other bytes). An unreadable answer fails the job too, never reads as
  "absent". A published npm version can never be replaced (npm unpublish is
  limited and the number stays burned): fix forward with the next rc.
- GitHub Packages may not return `dist.integrity` for an existing version;
  `registry-state` then fails ("no dist.integrity"), so a **re-run of
  `publish-nodejs-ghp` after a partial publish fails closed** instead of
  skipping. Checked on the first rc (B14); if it happens, fix forward with
  the next rc.
- The `node-packages` artifact is kept 30 days on a `nodejs/v*` tag run (1
  day elsewhere). **Approve the `nodejs-release` deployment and finish any
  re-run within that window**; after it the publish jobs cannot download the
  tarballs (a rebuild would not be the tested bytes) — fix forward with the
  next rc.
- **Required protection: environment `nodejs-release`** (`publish-nodejs-npmjs`
  runs in it; the owner created it, this repository does not): the
  deployment rule admits **only tags matching `nodejs/v*`** — it must include
  `nodejs/v*`, or every Node release job waits forever or is rejected — and
  `can_admins_bypass` is **false**. **No required reviewer** (owner decision,
  2026-11-04, after the rc.1 publish): pushing a `nodejs/v*` tag publishes
  with no approval click; the gate is who can create the tag (the ruleset,
  admins only) and the org's required 2FA. A tag ruleset protects `nodejs/v*`, `v*`,
  `dotnet/v*` and `go/**/*` (`refs/tags/go/**/*`, the Go core and ext tags).
  Check before every Node release:
  ```bash
  gh api repos/StaffOps/otel-libs/environments/nodejs-release \
    --jq '{rules: [.protection_rules[].type], policy: .deployment_branch_policy}'   # expect rules ["branch_policy"]
  gh api repos/StaffOps/otel-libs/environments/nodejs-release/deployment-branch-policies \
    --jq '.branch_policies[] | {name, type}'        # expect {"name":"nodejs/v*","type":"tag"}
  gh api repos/StaffOps/otel-libs/environments/nodejs-release --jq .can_admins_bypass   # expect false
  gh api repos/StaffOps/otel-libs/rulesets --jq '.[] | {id, name, target, enforcement}'
  gh api repos/StaffOps/otel-libs/rulesets/<id> --jq .conditions.ref_name.include
  # expect refs/tags/nodejs/v*, refs/tags/v*, refs/tags/dotnet/v*, refs/tags/go/**/*
  ```
  Expect `required_reviewers` among the rules and a custom policy.

## Node.js first publish — one-time owner bootstrap
**DONE 2026-11-04** (owner; verified the same day): the six packages exist
on npmjs with `0.0.0-bootstrap.0` (dist-tags `latest` + `bootstrap`,
deprecated), each has its trusted publisher (checked with `npm trust list`)
and "Require two-factor authentication and disallow tokens". Each package
also shows `0.0.0-stage`, a placeholder npm itself creates on a new
package's first publish ("Temporary package placeholder for staged
publishing"): leave it alone. Do not run these steps again; they stay here
for a new package or a new scope.

npm accepts a trusted publisher only for a package that already exists. The
bootstrap publishes a throwaway version, `0.0.0-bootstrap.0`, of each of the
six packages by hand and deprecates it; every real version is then
published by CI only, through OIDC and with provenance. The agent never runs
these steps and never sees a credential.

**Preconditions.** The repository `StaffOps/otel-libs` is **public**
(npmjs rejects provenance from a private repository with E422, so the CI
publish would fail); the environment `nodejs-release` and the tag ruleset
exist (above; owner, 2026-11-04); the npm account has 2FA in
**"authorization and writes"** mode — without it every publish fails with
E403 "Two-factor authentication or granular access token with bypass 2fa
enabled is required"; `docker` works. The image is pinned by digest:
`node:24.22.2-slim@sha256:c86ecead9e55a5210ac1727de38a4ca840bf03dcaf34dc9749addbf383b1920c`.

1. **Scratch clone of the branch or commit that will be tagged** — not your
   working copy (the containers write Linux `node_modules/` and `tarballs/`,
   and the version change must never be committed). Set `BRANCH` to the
   branch that carries `nodejs/` and will be tagged (`main` once `nodejs/` is
   merged; until then the release branch — `git ls-remote --heads origin`
   lists them) and, optionally, `COMMIT` to the exact commit you will tag:
   ```bash
   BRANCH='<branch to be tagged>'   # e.g. main, once it holds nodejs/
   COMMIT=''                        # optional: the exact commit to tag
   git clone -b "$BRANCH" https://github.com/StaffOps/otel-libs.git /tmp/otel-libs-bootstrap
   cd /tmp/otel-libs-bootstrap
   [ -z "$COMMIT" ] || git checkout --detach "$COMMIT"
   test -d nodejs || { echo "no nodejs/ on $BRANCH: wrong branch, stop here" >&2; false; }
   IMG='node:24.22.2-slim@sha256:c86ecead9e55a5210ac1727de38a4ca840bf03dcaf34dc9749addbf383b1920c'
   ```
2. **Set the throwaway version** (uncommitted):
   ```bash
   docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD/nodejs:/w" -w /w "$IMG" node ci/version.mjs set 0.0.0-bootstrap.0
   ```
3. **Build and pack**:
   ```bash
   docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD:/repo" -w /repo/nodejs "$IMG" sh -ec '
     npm ci --ignore-scripts && npm run build
     rm -rf tarballs && mkdir tarballs
     npm pack --workspaces --pack-destination tarballs
     node ci/version.mjs check 0.0.0-bootstrap.0 --tarballs tarballs'
   ```
4. **Log in — web login, no token.** Open one interactive `--rm` container
   and keep it open until step 10; run it under `script` so every prompt
   keeps a TTY (macOS `script -q <log> <cmd…>`; util-linux
   `script -q -c '<cmd>' <log>`). The log holds the login URLs, never a
   credential; delete it at the end.
   ```bash
   script -q /tmp/otel-libs-bootstrap.log \
     docker run --rm -it --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD:/repo" -w /repo/nodejs "$IMG" bash
   # inside the container:
   cfg=/tmp/npmrc; npmjs=https://registry.npmjs.org/; reg=--@staffops:registry=$npmjs
   npm login --auth-type=web --registry="$npmjs" --userconfig="$cfg"   # open the URL, approve with 2FA
   npm whoami --registry="$npmjs" --userconfig="$cfg"                  # must print the owner account; stop otherwise
   ```
   Always `--userconfig="$cfg"` (with `=`): `npm trust list` rejects
   `--userconfig <path>` with a space ("Unknown positional argument").
   **Token only as a fallback** (the token path failed twice in the 2026-11-04
   bootstrap: ENEEDAUTH when a pasted `read` swallowed the next line, then
   E401). If you must: npmjs → Access Tokens → Granular, packages and scopes
   read and write, scope `@staffops` only, organizations none, expiry
   **1 day**, bypass 2FA off. Paste it into `read` on a line of its own, check
   the length before anything else, then pass it to the container
   (`-e NPM_BOOTSTRAP_TOKEN`) and use it instead of `npm login`; never write
   the value to a file, the repository, a CI secret or a chat:
   ```bash
   read -rs NPM_BOOTSTRAP_TOKEN && export NPM_BOOTSTRAP_TOKEN   # its own line; paste; not echoed
   echo ${#NPM_BOOTSTRAP_TOKEN}                                 # the length only; 0 or a short number = stop
   # inside the container, instead of npm login (npm expands the variable name itself):
   printf "%s\n" "//registry.npmjs.org/:_authToken=\${NPM_BOOTSTRAP_TOKEN}" > "$cfg"
   npm whoami --registry="$npmjs" --userconfig="$cfg"
   ```
5. **Publish ×6 with `--tag bootstrap`, wait until readable, then deprecate
   ×6**, in the same container. Each write asks for a web 2FA confirmation
   (expect 12: one per publish, one per deprecate). A freshly published
   package can return 404 on read for a few minutes, so `npm deprecate`
   right after the publish fails E404: wait until all six answer 200.
   ```bash
   pkgs='otel-helper otel-helper-sql otel-helper-redis otel-helper-aws otel-helper-pino otel-helper-winston'
   for t in $(node ci/version.mjs tarballs 0.0.0-bootstrap.0); do
     npm publish "./tarballs/$t" --access public --tag bootstrap --userconfig="$cfg" "$reg"
   done
   for p in $pkgs; do   # same as: curl -so /dev/null -w '%{http_code}' https://registry.npmjs.org/@staffops%2f<p>
     until node -e "fetch('https://registry.npmjs.org/@staffops%2f$p').then(r=>process.exit(r.status===200?0:1),()=>process.exit(1))"; do
       echo "waiting for @staffops/$p"; sleep 30
     done
   done
   for p in $pkgs; do
     npm deprecate "@staffops/$p@0.0.0-bootstrap.0" "bootstrap placeholder for trusted publishing; do not install" \
       --userconfig="$cfg" "$reg"
   done
   ```
6. **Token fallback only: revoke it** on npmjs right away (Access Tokens →
   Delete), even though it expires in a day, and `unset NPM_BOOTSTRAP_TOKEN`
   on the host. Keep the container open.
7. **Environment protection**: already in place (created by the owner,
   2026-11-04: `can_admins_bypass=false`, the owner as required reviewer
   until the rc.1 publish, then removed (see "Required protection"),
   tag policy `nodejs/v*`; org 2FA required; ruleset `release-tags` with
   `refs/tags/go/**/*`). Re-run the `gh api` checks of the section above.
8. **Trusted publisher, per package** (all six): npmjs → package → Settings
   → Trusted Publisher → GitHub Actions: organization or user `StaffOps`,
   repository `otel-libs`, workflow filename `ci.yml`, environment
   `nodejs-release`. **Tick "Allow npm publish"**; leave "Allow npm
   dist-tag" off. CI runs `npm publish --provenance --access public --tag
   <latest|next>` (`ci.yml` `publish-nodejs-npmjs`) and never `npm
   dist-tag`. npm shows "Not recommended" next to "Allow npm publish" and
   recommends staged publishing; the owner's decision (2026-11-04, design
   D-JS11 amendment): no manual npm approval per release — the human gate is
   the tag push itself (admins only, by the tag ruleset; the environment
   reviewer was removed the same day) plus the org
   2FA.
9. **Lock publishing**: in the same page, Publishing access → "Require
   two-factor authentication and disallow tokens" — from now on only the
   trusted publisher (and the owner with 2FA) can publish.
10. **Check**, still in the container (each `npm trust list` asks for a web
    2FA confirmation; do not add `--json`: npm then masks the web-auth URL
    as `***` and you cannot approve it):
    ```bash
    for p in $pkgs; do
      npm trust list "@staffops/$p" --userconfig="$cfg" "$reg"
      npm view "@staffops/$p" dist-tags deprecated "$reg"
    done
    npm logout --registry="$npmjs" --userconfig="$cfg"
    exit
    ```
    Each trust list shows github, file `ci.yml`, repository
    `StaffOps/otel-libs`, environment `nodejs-release`, permissions
    "publish, stage publish". Each package shows `latest` and `bootstrap` =
    `0.0.0-bootstrap.0` (npm points `latest` at a package's first version
    whatever `--tag` says) and the deprecation message. Then delete the log
    and the scratch clone.

**Until the first stable (non-rc) release, `latest` is the deprecated
bootstrap version** (release candidates go to `next`): a bare `npm install
@staffops/otel-helper` is not safe — always install an exact version
(`CONSUMING.md`). GitHub Packages needs no bootstrap: `publish-nodejs-ghp`
creates the packages on the first tagged run, linked to the repository by
each `package.json` `repository` field.

## Node.js SDK-bump checklist
Before raising any `@opentelemetry/*` range in `nodejs/packages/*/package.json`
(the SDK 3.x / experimental 0.3xx packages move as a matched set, design
D-JS2) or the `@opentelemetry/api` peer range:
- **Exporter environment (design D-JS3).** For every OTLP exporter and
  `otlp-grpc-exporter-base` release in the bump, list the `OTEL_EXPORTER_OTLP_*`
  variables it reads and when: each must be in `MASKED_SUFFIXES`
  (`otel-helper/src/internal/exporters.ts`) or passed explicitly, and none
  may be read after construction (the mask is lifted then). Re-run the
  parity export cases and the e2e smoke (both Node legs).
- **`Span.status` writable in `onEnding`** (sdk-trace): the
  `GrpcServerStatusProcessor` sets the server span status there — test "A22
  gRPC server status through a real TracerProvider".
- **`InstrumentationBase` still enables in its constructor**
  (`@opentelemetry/instrumentation`), which the opt-in contrib registry
  relies on (B6 final review).
- **API proxies (G1 rollback, `setup.ts` `releaseGlobal`).** The `logs` API's
  private `_proxyLoggerProvider._setDelegate` and the trace
  `ProxyTracerProvider`'s `setDelegate` still exist with the same shape —
  test "M3" in `test/setup.test.cjs`; otherwise the rollback falls back to
  `disable()` (B9 final round).
- **sdk-logs `_flushAll` early return** (`BatchLogRecordProcessorBase`,
  design D-JS14): the helper serializes bridge flushes per LoggerProvider;
  re-check whether upstream fixed it — tests "HIGH-2" in
  `test/logging.test.cjs`.
- Run the whole Node CI set locally: `testing/verify.sh node` on 24, with
  `NODE_VERSION=22.15.0` and `26`; `sh ci/api-range-install.sh`;
  `SMOKE_LANGS=node NODE_VERSION=22.15.0 testing/e2e/smoke.sh`.

## .NET SDK-bump checklist
Before raising any `OpenTelemetry*` `PackageReference` in `dotnet/` (core and
contrib move together on one generation, requirement DN1; first walked in
task C3, 1.15.x → 1.20.x):
- **Constructor and options-factory env reads.** For every options type the
  helper configures (`OtlpExporterOptions`, `PrometheusHttpListenerOptions`,
  `SdkLimitOptions`, instrumentation options), list the `OTEL_*` variables the
  new version reads from `IConfiguration`/env and when. The helper's configure
  delegate runs after the options factory: a value it sets wins, a value it
  leaves alone is now env-controlled. Example: the Prometheus listener reads
  `OTEL_EXPORTER_PROMETHEUS_HOST`/`_PORT` since 1.16.0-beta.1, and `HOST=*`
  aborts the provider build unless the helper sets `Host` (C3,
  `MetricsSetup.ConfigurePrometheusListener`).
- **Default client hardening.** Diff the exporter's default `HttpClient`
  (redirects, response size, timeout, TLS) against what the helper's own
  client does (ADR-013 duties list, C8). 1.20.1 stopped following redirects
  only for clients the exporter creates itself; an app's `IHttpClientFactory`
  client still follows them (measured in C3).
- **Experimental flags.** List the `OTEL_DOTNET_EXPERIMENTAL_*` switches the
  new versions add or change (AspNetCore gRPC spans, new root span, SqlClient
  returned rows): the helper never sets them; the migration guide says which
  ones change telemetry.
- **Breaking entries.** Re-read each package's CHANGELOG at its tag (core
  `core-<v>`, Prometheus `coreunstable-<v>`, contrib `Instrumentation.<X>-<v>`)
  and walk every **Breaking** line with a before/after measurement.
- **Telemetry diff.** Before/after diff of the three examples (spans, OTLP
  metrics, logs, the `/metrics` exposition) on the same runtime, then
  `testing/e2e/dotnet-prom-scrape.sh` (scrape from another container).
- The suite on `sdk:8.0` with coverage (`dotnet/TESTS.md`), `dotnet pack` of
  the five packages, and a consumer of the packed packages against a
  throwaway Collector.

## Tagging (the owner pushes tags; auto mode blocks it for the agent)
- Annotated tags on the exact commit.
- **Never push more than 3 tags in one `git push`** — GitHub then creates no
  push events and no workflow runs (rc.1 lost its CI this way). Push the
  Python tag alone, then the Go tags; push a `nodejs/v*` tag on its own too.
- Give the owner the commands as `! cd <repo> && …` lines.

## After tagging
- `gh run list` → watch the tag run (`gh run watch <id> --exit-status`).
- `gh release view vX.Y.Z… --json isPrerelease,assets`: pre-release tags must
  be **Pre-release**, not Latest (the job passes `--prerelease
  --latest=false` for tags containing `-`; check anyway).
- **Node.js** (after the tag's publish jobs finish; no approval step):
  - attestations: `npm view @staffops/otel-helper@<v> dist.attestations` is
    present for all six, and each npmjs page shows "Provenance";
  - dist-tags: `npm view @staffops/otel-helper dist-tags` — an rc on `next`,
    a stable release on `latest`;
  - GitHub Packages: the six packages at `<v>` on the repository's Packages page;
  - install smoke in a throwaway container: `npm init -y && npm install
    @staffops/otel-helper@<v> @staffops/otel-helper-sql@<v>
    @opentelemetry/api@~1.10.0 && node -e "require('@staffops/otel-helper')"
    && npm audit signatures`;
  - anything wrong: fix forward with the next rc (a published version
    cannot be replaced).
- If the run fails: fix, bump to the next rc (do not move a published tag),
  record the failed rc in the CHANGELOG.

## Final (non-rc) release
Requires the rc soak (`specs/soak-simulation/`) acceptance and D11
prerequisites tracked (A12). Rename the CHANGELOG `[Unreleased]` content to
`[X.Y.Z] - YYYY-MM-DD` and open a fresh `[Unreleased]`.
