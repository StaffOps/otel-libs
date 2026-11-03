# Build from the repo root (context nodejs/):
#   docker build -t node-custom-signals -f nodejs/example/node-custom-signals/Dockerfile nodejs
# Installs @staffops/otel-helper from the local workspace source: built and
# packed in the first stage, then installed like a published package.

FROM node:24.22.2-slim AS helper
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
RUN npm run build --workspace=@staffops/otel-helper \
 && npm pack --workspace=@staffops/otel-helper --pack-destination /tarballs

FROM node:24.22.2-slim
ENV NODE_ENV=production
WORKDIR /app

# App dependencies, exact versions from the example's lockfile. The helper is
# not listed in package.json on purpose (the @staffops scope is not claimed on
# npmjs yet, so a registry entry could resolve to someone else's package); its
# own dependencies resolve within its declared ranges, as Python's wheel install.
COPY example/node-custom-signals/package.json example/node-custom-signals/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund && npm cache clean --force
COPY --from=helper /tarballs/ /tmp/tarballs/
RUN npm install --no-save --omit=dev --ignore-scripts --no-audit --no-fund /tmp/tarballs/staffops-otel-helper-*.tgz \
 && npm cache clean --force && rm -rf /tmp/tarballs

COPY example/node-custom-signals/custom-signals.js ./

USER node
CMD ["node", "custom-signals.js"]
