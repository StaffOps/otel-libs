# Build from the repo root (context nodejs/):
#   docker build -t node-custom-signals -f nodejs/example/node-custom-signals/Dockerfile nodejs
# Installs the published @staffops/otel-helper pinned in package.json, with the
# dependency tree locked in package-lock.json (npm ci). Against the local tree
# instead (a commit under development):
#   docker build --build-arg HELPER_SOURCE=workspace -t node-custom-signals -f nodejs/example/node-custom-signals/Dockerfile nodejs
# builds and packs the helper from the workspace source and installs that
# tarball over the published one (package.json and the lockfile are not changed).
ARG HELPER_SOURCE=registry

# HELPER_SOURCE=registry: nothing to install over the published helper.
FROM node:24.22.2-slim AS helper-registry
RUN mkdir /tarballs

# HELPER_SOURCE=workspace: the core built and packed from the workspace source.
FROM node:24.22.2-slim AS helper-workspace
WORKDIR /src
# Manifests first, so the dependency layer is reused until a lockfile changes.
# The workspace manifests are listed by hand (COPY cannot glob while keeping the
# directories): a new package under packages/ needs its line here, or the
# install no longer matches the workspace and its lockfile.
COPY package.json package-lock.json .npmrc tsconfig.base.json ./
COPY packages/otel-helper/package.json packages/otel-helper/
COPY packages/otel-helper-aws/package.json packages/otel-helper-aws/
COPY packages/otel-helper-pino/package.json packages/otel-helper-pino/
COPY packages/otel-helper-redis/package.json packages/otel-helper-redis/
COPY packages/otel-helper-sql/package.json packages/otel-helper-sql/
COPY packages/otel-helper-winston/package.json packages/otel-helper-winston/
# The whole workspace: the core build needs the root typescript and @types/node.
RUN npm ci --ignore-scripts --no-audit --no-fund && npm cache clean --force
COPY packages/ ./packages/
# npm pack does not create the destination directory (npm 11).
RUN mkdir /tarballs && npm run build --workspace=@staffops/otel-helper \
 && npm pack --workspace=@staffops/otel-helper --pack-destination /tarballs

# Only the selected stage is built (BuildKit); another HELPER_SOURCE value fails the
# build (BuildKit tries to pull an image named helper-<value>).
FROM helper-${HELPER_SOURCE} AS helper

FROM node:24.22.2-slim
ENV NODE_ENV=production
WORKDIR /app

# App dependencies and the published @staffops/otel-helper, exact versions and
# integrity from the example's lockfile (the helper's own dependencies included).
COPY example/node-custom-signals/package.json example/node-custom-signals/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund && npm cache clean --force
# HELPER_SOURCE=workspace only: the local core tarball replaces the published
# helper (--no-save: package.json and the lockfile keep the published pin). Its
# dependencies are re-resolved within its declared ranges, reusing the locked tree where it fits.
COPY --from=helper /tarballs/ /tmp/tarballs/
RUN set -- /tmp/tarballs/*.tgz; \
    if [ -e "$1" ]; then npm install --no-save --omit=dev --ignore-scripts --no-audit --no-fund "$@" && npm cache clean --force; fi; \
    rm -rf /tmp/tarballs

COPY example/node-custom-signals/custom-signals.js ./

USER node
CMD ["node", "custom-signals.js"]
