#!/bin/sh
# B1c spike (specs/nodejs-otel-helper/spike-B1c.md), from the repo root:
#   NODE_VERSION=<22.15.0|22|24|26> docker compose -f testing/docker-compose.yml \
#     run --rm node-tests sh /src/nodejs/spike/run-in-docker.sh
# Copies the workspace out of the read-only /src mount, builds and packs the
# core, installs the spike's own lockfile plus the packed core (--no-save), maps
# the receiver's DNS SAN `fakeotlp` to 127.0.0.2 (finding F5) and runs every check.
set -eu
mkdir -p /work
tar -C /src/nodejs --exclude=node_modules --exclude=dist --exclude=coverage --exclude='*.tgz' -cf - . | tar -C /work -xf -
cd /work
npm ci --ignore-scripts --loglevel=error
npm run build --silent
grep -q ' fakeotlp$' /etc/hosts || echo '127.0.0.2 fakeotlp' >> /etc/hosts
dir="$(mktemp -d)"
npm pack ./packages/otel-helper --pack-destination "$dir" --loglevel=error >/dev/null
cd spike
npm ci --ignore-scripts --loglevel=error
npm install --no-save --ignore-scripts --loglevel=error "$dir"/staffops-otel-helper-*.tgz
echo "arch: $(node -p 'process.arch'); node $(node -v)"
node run-spike.cjs
