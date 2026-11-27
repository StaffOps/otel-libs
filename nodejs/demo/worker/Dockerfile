# demo-node-worker (specs/demo-environment DE3c). Build from the repository root:
#   docker build --platform linux/amd64 -t otel-libs-demo-node-worker:dev -f nodejs/demo/worker/Dockerfile .
# The root context sees the Node.js workspace (the worker needs no contract file:
# it speaks HTTP and Redis only); Dockerfile.dockerignore next to this file limits it to those.
#
# Dependencies: exact versions and integrity from nodejs/demo/package-lock.json
# (npm ci), then the helper packages built and packed from the workspace source
# replace the published pins (npm install --no-save): the demo always runs the
# helper code of this commit.

# Base image pinned by digest (DE-D7 r3.5).
FROM node:22.23.4-slim@sha256:5ec23b7c622b00547753bc6ffd78085dd868a68aeb41c8e0a1418f033e13d96d AS helper
WORKDIR /src
# Workspace manifests first (listed by hand: a new package under packages/ needs its line).
COPY nodejs/package.json nodejs/package-lock.json nodejs/.npmrc nodejs/tsconfig.base.json ./
COPY nodejs/packages/otel-helper/package.json packages/otel-helper/
COPY nodejs/packages/otel-helper-aws/package.json packages/otel-helper-aws/
COPY nodejs/packages/otel-helper-pino/package.json packages/otel-helper-pino/
COPY nodejs/packages/otel-helper-redis/package.json packages/otel-helper-redis/
COPY nodejs/packages/otel-helper-sql/package.json packages/otel-helper-sql/
COPY nodejs/packages/otel-helper-winston/package.json packages/otel-helper-winston/
RUN npm ci --ignore-scripts --no-audit --no-fund && npm cache clean --force
COPY nodejs/packages/ ./packages/
# The core first: the opt-in packages compile against its types. npm pack does
# not create the destination directory (npm 11).
RUN mkdir /tarballs \
 && npm run build --workspace=@staffops/otel-helper \
 && npm run build --workspace=@staffops/otel-helper-pino --workspace=@staffops/otel-helper-redis \
 && npm pack --pack-destination /tarballs --workspace=@staffops/otel-helper \
      --workspace=@staffops/otel-helper-pino --workspace=@staffops/otel-helper-redis

FROM node:22.23.4-slim@sha256:5ec23b7c622b00547753bc6ffd78085dd868a68aeb41c8e0a1418f033e13d96d
ENV NODE_ENV=production
WORKDIR /app
COPY nodejs/demo/package.json nodejs/demo/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund && npm cache clean --force
# The locked tree before the workspace tarballs go in.
RUN npm ls --omit=dev --all --parseable --long > /tmp/deps-locked.txt
COPY --from=helper /tarballs/ /tmp/tarballs/
# Drift check: after the tarballs replace the published @staffops packages,
# `npm ls` must still be clean and every other package must be exactly the
# locked one (a workspace dependency change that needs a new lockfile fails
# the build instead of resolving silently).
RUN npm install --no-save --omit=dev --ignore-scripts --no-audit --no-fund /tmp/tarballs/*.tgz \
 && npm ls --omit=dev --all --parseable --long > /tmp/deps-installed.txt \
 && for f in locked installed; do grep -v -E '^[^:]*/node_modules/@staffops/[^/:]+:' /tmp/deps-$f.txt | sort > /tmp/deps-$f.cmp; done \
 && if ! cmp -s /tmp/deps-locked.cmp /tmp/deps-installed.cmp; then \
      echo 'dependency drift after installing the workspace helper tarballs:'; diff /tmp/deps-locked.cmp /tmp/deps-installed.cmp; exit 1; fi \
 && npm cache clean --force && rm -rf /tmp/tarballs /tmp/deps-*

COPY nodejs/demo/lib/ /app/lib/
COPY nodejs/demo/worker/main.js nodejs/demo/worker/app.js /app/worker/

USER node
CMD ["node", "worker/main.js"]
