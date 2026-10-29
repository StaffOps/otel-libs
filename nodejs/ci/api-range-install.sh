#!/bin/sh
# Install test (spec JS1, D-JS2): pack the built core, install it in a fresh
# consumer next to the NEWEST @opentelemetry/api its peer range admits and the
# app's own @grpc/grpc-js, check the tree is valid, that the packages which
# must be shared have exactly one copy (`npm ls` exits 0 with duplicates, so
# paths are counted), and that both `require` and `import` load the core.
# Needs the npm registry; NODE_TESTS_OFFLINE=1 skips it. Run from nodejs/
# after `npm ci && npm run build`:
#   sh ci/api-range-install.sh
set -eu
if [ "${NODE_TESTS_OFFLINE:-}" = 1 ]; then
  echo "api-range install test: SKIPPED (NODE_TESTS_OFFLINE=1)"
  exit 0
fi
ws="$(pwd)"
range="$(node -p "require('./packages/otel-helper/package.json').peerDependencies['@opentelemetry/api']")"
newest="$(npm view "@opentelemetry/api@$range" version --json | node -e '
  let s = ""; process.stdin.on("data", (d) => (s += d)).on("end", () => {
    const v = JSON.parse(s); console.log(Array.isArray(v) ? v[v.length - 1] : v);
  });')"
echo "api peer range: $range -> newest admitted: $newest"

dir="$(mktemp -d)"
trap 'rm -rf "$dir"' EXIT
npm pack ./packages/otel-helper --pack-destination "$dir" --loglevel=error >/dev/null
cd "$dir"
printf '{ "name": "api-range-consumer", "private": true }\n' > package.json
npm install --ignore-scripts --no-audit --no-fund --loglevel=error ./staffops-otel-helper-*.tgz \
  "@opentelemetry/api@$newest" "@grpc/grpc-js@^1.14.5"
npm ls --all >/dev/null
# The api package does not export ./package.json; read the installed file.
installed="$(node -p "JSON.parse(require('fs').readFileSync('node_modules/@opentelemetry/api/package.json', 'utf8')).version")"
echo "installed api: $installed"
[ "$installed" = "$newest" ] || { echo "FAIL: installed api $installed, expected $newest"; exit 1; }
if [ "${PLANT_DUPLICATE:-}" = 1 ]; then  # self-test of the copy count (review aid)
  mkdir -p node_modules/@opentelemetry/core/node_modules/import-in-the-middle
  cp node_modules/import-in-the-middle/package.json node_modules/@opentelemetry/core/node_modules/import-in-the-middle/
fi
status=0
for p in @opentelemetry/api @opentelemetry/core @opentelemetry/instrumentation \
         @opentelemetry/otlp-grpc-exporter-base @grpc/grpc-js import-in-the-middle; do
  n="$(find . -path "*/node_modules/$p/package.json" | wc -l | tr -d ' ')"
  echo "copies of $p: $n"
  [ "$n" = 1 ] || status=1
done
[ "$status" = 0 ] || { echo "FAIL: a package that must be shared is not installed exactly once"; exit 1; }
node -e "const h = require('@staffops/otel-helper'); if (!h.VERSION) process.exit(1)"
node --input-type=module -e "import { VERSION } from '@staffops/otel-helper'; if (!VERSION) process.exit(1)"
echo "api-range install test: OK (workspace $ws)"
