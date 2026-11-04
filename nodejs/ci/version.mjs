// Release version tool for the Node.js packages (task B13, requirement JS10).
// Every place that carries the release version must agree, so the six packages
// are always published with identical versions and exact peer pins on the core:
//   - packages/*/package.json `version`;
//   - the opt-in packages' `peerDependencies["@staffops/otel-helper"]` (exact);
//   - the `VERSION` constants (core, -pino and -winston src/version.ts, -sql/-redis/-aws src/internal/*-instrumentation.ts);
//   - package-lock.json, the `packages/<dir>` entries (version and core peer);
//   - example/*/Dockerfile.demo `ARG HELPER_VERSION=` (the default tarball name).
// The version mentions in README.md and packages/*/README.md that match DOC_RES
// ("Not released yet (`<version>`)", tarball names) are rewritten by `set` and
// only warned about by `check`: their wording changes at release time. Other
// README text is never touched.
//
// Run from nodejs/ (or pass --root <dir>):
//   node ci/version.mjs check <version | nodejs/vX.Y.Z> [--tarballs <dir>]
//   node ci/version.mjs set <version>
//   node ci/version.mjs dist-tag <version | nodejs/vX.Y.Z>    # "latest", or "next" for a pre-release
//   node ci/version.mjs tarballs <version | nodejs/vX.Y.Z>    # tarball names, publish order (core first)
//   node ci/version.mjs verify-sums <version | tag> --tarballs <dir> --sums <file>
//   node ci/version.mjs registry-state <tarball> --registry <url> [--userconfig <file>]
// `check --tarballs <dir>` also reads package/package.json inside each of the six
// npm pack tarballs (name, version, core peer). `verify-sums` requires a
// sha256sum-format file listing exactly the six tarballs, every hash matching,
// and no other .tgz in the directory. `registry-state` prints `absent`, `same`
// (the registry holds this version with this tarball's sha512 integrity) or
// `different` (it holds this version with other bytes); it fails when the
// registry answer cannot be read or holds no integrity. NPM_BIN overrides the
// npm executable (tests). Exit 1 on any mismatch or failure, 2 on bad usage.
import { createHash } from 'node:crypto';
import { execFileSync, spawnSync } from 'node:child_process';
import { existsSync, readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import path from 'node:path';

export const CORE = '@staffops/otel-helper';

/** Package directories in publish order: the core first, every opt-in package peer-depends on it. */
export const PACKAGES = [
  'otel-helper',
  'otel-helper-sql',
  'otel-helper-redis',
  'otel-helper-aws',
  'otel-helper-pino',
  'otel-helper-winston',
];

/** Source files holding `export const VERSION = '<version>'`, per package directory. */
export const VERSION_CONSTANTS = {
  'otel-helper': 'src/version.ts',
  'otel-helper-sql': 'src/internal/sql-instrumentation.ts',
  'otel-helper-redis': 'src/internal/redis-instrumentation.ts',
  'otel-helper-aws': 'src/internal/aws-instrumentation.ts',
  'otel-helper-pino': 'src/version.ts',
  'otel-helper-winston': 'src/version.ts',
};

const USAGE = 'usage: node ci/version.mjs <check | set | dist-tag | tarballs | verify-sums | registry-state> <version | nodejs/vX.Y.Z | tarball> [--root <dir>] [--tarballs <dir>] [--sums <file>] [--registry <url>] [--userconfig <file>]';

// SemVer 2.0.0 (semver.org), without build metadata: npm ignores it on publish.
const SEMVER = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?$/;
const CONST_RE = /export const VERSION = '([^']*)';/;
const ARG_RE = /^ARG HELPER_VERSION=(.*)$/m;
const DOC_RES = [/Not released yet\*?\*? \(`([^`]+)`\)/g, /staffops-otel-helper(?:-[a-z]+)?-(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+?)?)\.tgz/g];

/** Error carrying usage semantics (exit 2). */
class UsageError extends Error {}

/** Accepts `X.Y.Z[-pre]` or the tag `nodejs/vX.Y.Z[-pre]`; throws on anything else. */
export function parseVersion(input) {
  const raw = String(input ?? '');
  const version = raw.startsWith('nodejs/v') ? raw.slice('nodejs/v'.length) : raw;
  if (!SEMVER.test(version)) {
    throw new UsageError(`not a release version (want X.Y.Z[-pre] or nodejs/vX.Y.Z[-pre]): ${JSON.stringify(raw)}`);
  }
  return version;
}

/** npm requires an explicit dist-tag for a pre-release: never let one become "latest". */
export function distTag(version) {
  return version.includes('-') ? 'next' : 'latest';
}

/** `npm pack` file name of a scoped package: `@staffops/<dir>` -> `staffops-<dir>-<version>.tgz`. */
export function tarballName(dir, version) {
  return `staffops-${dir}-${version}.tgz`;
}

function readText(file) {
  try {
    return readFileSync(file, 'utf8');
  } catch (err) {
    throw new Error(`cannot read ${file}: ${err.message}`, { cause: err });
  }
}
function readJson(file) {
  const text = readText(file);
  try {
    return JSON.parse(text);
  } catch (err) {
    throw new Error(`${file}: invalid JSON: ${err.message}`, { cause: err });
  }
}
const writeJson = (file, value) => writeFileSync(file, `${JSON.stringify(value, null, 2)}\n`);
const isDir = (p) => existsSync(p) && statSync(p).isDirectory();
const subdirs = (dir) => (isDir(dir) ? readdirSync(dir).filter((d) => isDir(path.join(dir, d))).sort() : []);
const dockerfiles = (root) =>
  subdirs(path.join(root, 'example'))
    .map((d) => path.join(root, 'example', d, 'Dockerfile.demo'))
    .filter((f) => existsSync(f));
const readmes = (root) =>
  [path.join(root, 'README.md'), ...PACKAGES.map((d) => path.join(root, 'packages', d, 'README.md'))].filter((f) => existsSync(f));

/** Every version-carrying location as [label, found, want?] rows (found: the string read, or undefined). */
export function collect(root) {
  const out = [];
  const onDisk = subdirs(path.join(root, 'packages'));
  const expected = [...PACKAGES].sort();
  if (onDisk.join(',') !== expected.join(',')) {
    out.push(['packages/ directory set', onDisk.join(','), `${expected.join(',')} (update PACKAGES in ci/version.mjs)`]);
  }
  const lock = readJson(path.join(root, 'package-lock.json'));
  for (const dir of PACKAGES) {
    const pkgFile = path.join(root, 'packages', dir, 'package.json');
    if (!existsSync(pkgFile)) continue;
    const pkg = readJson(pkgFile);
    out.push([`packages/${dir}/package.json version`, pkg.version]);
    const lockEntry = lock.packages?.[`packages/${dir}`] ?? {};
    out.push([`package-lock.json packages/${dir} version`, lockEntry.version]);
    if (dir === 'otel-helper') continue;
    out.push([`packages/${dir}/package.json peerDependencies["${CORE}"]`, pkg.peerDependencies?.[CORE]]);
    out.push([`package-lock.json packages/${dir} peerDependencies["${CORE}"]`, lockEntry.peerDependencies?.[CORE]]);
  }
  for (const [dir, rel] of Object.entries(VERSION_CONSTANTS)) {
    const file = path.join(root, 'packages', dir, rel);
    const match = existsSync(file) ? CONST_RE.exec(readText(file)) : null;
    out.push([`packages/${dir}/${rel} VERSION`, match?.[1]]);
  }
  for (const file of dockerfiles(root)) {
    const match = ARG_RE.exec(readText(file));
    out.push([`${path.relative(root, file)} ARG HELPER_VERSION`, match?.[1]?.trim()]);
  }
  return out;
}

/** README version mentions that differ from `version` (warnings, never failures). */
export function docWarnings(root, version) {
  const out = [];
  for (const file of readmes(root)) {
    const text = readText(file);
    for (const re of DOC_RES) {
      for (const m of text.matchAll(re)) {
        if (m[1] !== version) out.push(`${path.relative(root, file)}: mentions ${JSON.stringify(m[1])}, the release is ${JSON.stringify(version)} (by hand)`);
      }
    }
  }
  return out;
}

/** The package.json inside an npm pack tarball. */
export function tarballManifest(file) {
  let text;
  try {
    text = execFileSync('tar', ['-xzOf', file, 'package/package.json'], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
  } catch (err) {
    throw new Error(`${file}: cannot read package/package.json: ${String(err.stderr || err.message).trim()}`, { cause: err });
  }
  try {
    return JSON.parse(text);
  } catch (err) {
    throw new Error(`${file}: package/package.json: invalid JSON: ${err.message}`, { cause: err });
  }
}

/** Name, version and core peer read from inside each npm pack tarball. */
export function collectTarballs(dir, version) {
  const out = [];
  for (const pkgDir of PACKAGES) {
    const file = path.join(dir, tarballName(pkgDir, version));
    const label = `tarball ${path.basename(file)}`;
    if (!existsSync(file)) {
      out.push([label, 'absent', 'present']);
      continue;
    }
    const manifest = tarballManifest(file);
    out.push([`${label} name`, manifest.name, `@staffops/${pkgDir}`]);
    out.push([`${label} version`, manifest.version]);
    if (pkgDir !== 'otel-helper') out.push([`${label} peerDependencies["${CORE}"]`, manifest.peerDependencies?.[CORE]]);
  }
  return out;
}

/** Mismatch lines; empty when every location carries `version`. */
export function check(root, version, tarballDir) {
  const rows = collect(root);
  if (tarballDir) rows.push(...collectTarballs(tarballDir, version));
  const problems = [];
  for (const [label, found, want = version] of rows) {
    if (found !== want) problems.push(`${label}: ${found === undefined ? '(not found)' : JSON.stringify(found)}, want ${JSON.stringify(want)}`);
  }
  return problems;
}

function replaceOnce(file, re, replacement) {
  const text = readText(file);
  if (!re.test(text)) throw new Error(`${file}: no match for ${re} — the file layout changed; update ci/version.mjs`);
  writeFileSync(file, text.replace(re, replacement));
}

/** Rewrites every location (README mentions of the current core version included) to `version`; returns the files changed. */
export function set(root, version) {
  const changed = [];
  const lockFile = path.join(root, 'package-lock.json');
  const lock = readJson(lockFile);
  const previous = readJson(path.join(root, 'packages', 'otel-helper', 'package.json')).version;
  for (const dir of PACKAGES) {
    const pkgFile = path.join(root, 'packages', dir, 'package.json');
    const pkg = readJson(pkgFile);
    pkg.version = version;
    if (dir !== 'otel-helper') {
      if (!pkg.peerDependencies?.[CORE]) throw new Error(`${pkgFile}: no peerDependencies["${CORE}"] to set`);
      pkg.peerDependencies[CORE] = version;
    }
    writeJson(pkgFile, pkg);
    changed.push(pkgFile);
    const entry = lock.packages?.[`packages/${dir}`];
    if (!entry) throw new Error(`${lockFile}: no packages/${dir} entry`);
    entry.version = version;
    if (dir !== 'otel-helper') {
      if (!entry.peerDependencies?.[CORE]) throw new Error(`${lockFile}: packages/${dir}: no peerDependencies["${CORE}"] to set`);
      entry.peerDependencies[CORE] = version;
    }
  }
  writeJson(lockFile, lock);
  changed.push(lockFile);
  for (const [dir, rel] of Object.entries(VERSION_CONSTANTS)) {
    const file = path.join(root, 'packages', dir, rel);
    replaceOnce(file, CONST_RE, `export const VERSION = '${version}';`);
    changed.push(file);
  }
  for (const file of dockerfiles(root)) {
    replaceOnce(file, ARG_RE, `ARG HELPER_VERSION=${version}`);
    changed.push(file);
  }
  if (previous && previous !== version) {
    // Only the DOC_RES mentions ("Not released yet (`<v>`)", tarball names) that
    // carry the previous version; any other text in a README is left alone.
    for (const file of readmes(root)) {
      const text = readText(file);
      let out = text;
      for (const re of DOC_RES) {
        out = out.replace(new RegExp(re.source, 'g'), (whole, found) => (found === previous ? whole.replace(found, version) : whole));
      }
      if (out === text) continue;
      writeFileSync(file, out);
      changed.push(file);
    }
  }
  return changed;
}

/** sha256 of a file, hex. */
const sha256 = (file) => createHash('sha256').update(readFileSync(file)).digest('hex');

/** Problems with a sha256sum-format file for the six tarballs of `version` in `dir`. */
export function verifySums(dir, version, sumsFile) {
  const problems = [];
  const listed = new Map();
  for (const line of readText(sumsFile).split('\n')) {
    if (!line.trim()) continue;
    const m = /^([0-9a-f]{64}) [ *](.+)$/.exec(line);
    if (!m) {
      problems.push(`${sumsFile}: not a sha256sum line: ${JSON.stringify(line)}`);
      continue;
    }
    listed.set(path.basename(m[2]), m[1]);
  }
  const expected = PACKAGES.map((d) => tarballName(d, version));
  for (const name of expected) {
    const file = path.join(dir, name);
    if (!listed.has(name)) problems.push(`${name}: not in ${sumsFile}`);
    else if (!existsSync(file)) problems.push(`${name}: listed but absent from ${dir}`);
    else if (sha256(file) !== listed.get(name)) problems.push(`${name}: sha256 differs from the one build-lib-node recorded`);
  }
  for (const name of listed.keys()) if (!expected.includes(name)) problems.push(`${name}: listed but not one of the six release tarballs`);
  if (isDir(dir)) {
    for (const name of readdirSync(dir)) if (name.endsWith('.tgz') && !expected.includes(name)) problems.push(`${name}: unexpected tarball in ${dir}`);
  }
  return problems;
}

/** sha512 SRI string, as npm records `dist.integrity`. */
export const integrity = (file) => `sha512-${createHash('sha512').update(readFileSync(file)).digest('base64')}`;

/**
 * What a registry holds for this tarball's name@version: `absent`, `same` or `different`.
 * Throws when the answer cannot be read (network, auth, no integrity): never guess "absent".
 */
export function registryState(tarball, { registry, userconfig, npmBin = process.env.NPM_BIN || 'npm' }) {
  if (!registry) throw new UsageError('registry-state needs --registry <url>');
  const { name, version } = tarballManifest(tarball);
  const args = ['view', `${name}@${version}`, 'version', 'dist.integrity', '--json', `--@staffops:registry=${registry}`];
  if (userconfig) args.push('--userconfig', userconfig);
  const res = spawnSync(npmBin, args, { encoding: 'utf8' });
  if (res.error) throw new Error(`npm view ${name}@${version} on ${registry}: ${res.error.message}`);
  const stdout = (res.stdout ?? '').trim();
  if (!stdout && res.status === 0) return 'absent'; // older npm: existing package, unknown version
  let answer;
  try {
    answer = JSON.parse(stdout);
  } catch {
    throw new Error(`npm view ${name}@${version} on ${registry}: exit ${res.status}, unreadable answer: ${JSON.stringify(stdout.slice(0, 200))}`);
  }
  if (answer?.error) {
    if (answer.error.code === 'E404') return 'absent';
    throw new Error(`npm view ${name}@${version} on ${registry}: ${answer.error.code}: ${answer.error.summary ?? ''}`);
  }
  if (res.status !== 0) throw new Error(`npm view ${name}@${version} on ${registry}: exit ${res.status}`);
  if (answer?.version !== version) throw new Error(`npm view ${name}@${version} on ${registry}: answered version ${JSON.stringify(answer?.version)}`);
  const remote = answer['dist.integrity'];
  if (!remote) throw new Error(`npm view ${name}@${version} on ${registry}: no dist.integrity, cannot compare the published bytes`);
  return remote === integrity(tarball) ? 'same' : 'different';
}

const FLAGS = ['--root', '--tarballs', '--sums', '--registry', '--userconfig'];

/** CLI entry; returns the exit code. */
export function main(argv, log = console.log, err = console.error) {
  const args = [...argv];
  const opts = {};
  for (const flag of FLAGS) {
    const i = args.indexOf(flag);
    if (i < 0) continue;
    const [, value] = args.splice(i, 2);
    if (value === undefined || value.startsWith('--')) throw new UsageError(`${flag} needs a value`);
    opts[flag.slice(2)] = value;
  }
  const unknown = args.find((a) => a.startsWith('-'));
  if (unknown) throw new UsageError(`unknown option ${unknown}`);
  const [command, input, ...rest] = args;
  if (rest.length) throw new UsageError(`unexpected arguments: ${rest.join(' ')}`);
  const root = path.resolve(opts.root ?? path.join(import.meta.dirname, '..'));
  switch (command) {
    case 'check': {
      const version = parseVersion(input);
      const problems = check(root, version, opts.tarballs);
      for (const w of docWarnings(root, version)) err(`WARN ${w}`);
      for (const p of problems) err(`MISMATCH ${p}`);
      if (problems.length) return 1;
      log(`ok   every Node.js version location is ${version}${opts.tarballs ? ` (tarballs in ${opts.tarballs} included)` : ''}`);
      return 0;
    }
    case 'set': {
      const version = parseVersion(input);
      for (const f of set(root, version)) log(`set  ${path.relative(root, f)} -> ${version}`);
      return 0;
    }
    case 'dist-tag':
      log(distTag(parseVersion(input)));
      return 0;
    case 'tarballs': {
      const version = parseVersion(input);
      for (const dir of PACKAGES) log(tarballName(dir, version));
      return 0;
    }
    case 'verify-sums': {
      const version = parseVersion(input);
      if (!opts.tarballs || !opts.sums) throw new UsageError('verify-sums needs --tarballs <dir> and --sums <file>');
      const problems = verifySums(opts.tarballs, version, opts.sums);
      for (const p of problems) err(`MISMATCH ${p}`);
      if (problems.length) return 1;
      log(`ok   the six tarballs match the recorded sha256 sums`);
      return 0;
    }
    case 'registry-state':
      if (!input) throw new UsageError('registry-state needs a tarball path');
      log(registryState(input, { registry: opts.registry, userconfig: opts.userconfig }));
      return 0;
    default:
      throw new UsageError(command ? `unknown command ${JSON.stringify(command)}` : 'no command');
  }
}

if (import.meta.filename === process.argv[1]) {
  try {
    process.exitCode = main(process.argv.slice(2));
  } catch (e) {
    console.error(`version.mjs: ${e.message}`);
    if (e instanceof UsageError) console.error(USAGE);
    process.exitCode = e instanceof UsageError ? 2 : 1;
  }
}
