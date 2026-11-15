# syntax=docker/dockerfile:1
# Node e2e smoke image (testing/e2e/node-smoke/smoke.cjs) for a Kubernetes Job.
# Build context: the repo tree root (testing/k8s/build-images.sh exports HEAD).
# The core package is built from the workspace, packed with `npm pack` and
# installed from the tarball, as a consumer installs it.
FROM node:24-slim AS build
ENV NPM_CONFIG_UPDATE_NOTIFIER=false NPM_CONFIG_FUND=false NPM_CONFIG_AUDIT=false
WORKDIR /work
COPY nodejs/ /work/
# The whole workspace: the core's build needs root devDependencies (typescript, @types/node).
RUN mkdir -p /pkg \
 && npm ci --ignore-scripts >/dev/null \
 && npm run --silent build --workspace=@staffops/otel-helper \
 && npm pack --workspace=@staffops/otel-helper --pack-destination /pkg >/dev/null \
 && mv /pkg/staffops-otel-helper-*.tgz /pkg/otel-helper.tgz
WORKDIR /app
# @opentelemetry/api: the core's peer dependency, inside its range (>=1.10.0 <1.11.0).
RUN cp /pkg/otel-helper.tgz . \
 && printf '{"name":"otel-libs-smoke-node","private":true,"dependencies":{"@staffops/otel-helper":"file:./otel-helper.tgz","@opentelemetry/api":"~1.10.0"}}\n' > package.json \
 && npm install --omit=dev --ignore-scripts >/dev/null \
 && rm otel-helper.tgz
COPY testing/e2e/node-smoke/smoke.cjs /app/smoke.cjs

FROM gcr.io/distroless/nodejs24-debian12:nonroot
WORKDIR /app
COPY --from=build /app /app
USER 65532:65532
# One run: span e2e-smoke-span, counter e2e.smoke.runs, WARN log e2e-smoke-log.
# Exits non-zero when the program fails (process.exitCode = 1).
ENTRYPOINT ["/nodejs/bin/node", "/app/smoke.cjs"]
