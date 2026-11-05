// Tests for ci/version.mjs (task B13). Run from nodejs/: node --test "ci/*.test.mjs"
import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import { chmodSync, cpSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { after, describe, it } from 'node:test';
import {
  CORE, PACKAGES, VERSION_CONSTANTS, check, collectExamples, distTag, docWarnings, examplePin, exampleWarnings, integrity, main,
  npmjsTarball, parseVersion, registryState, set, setExamples, tarballName, verifySums,
} from './version.mjs';

const ws = path.join(import.meta.dirname, '..');
const script = path.join(import.meta.dirname, 'version.mjs');
const current = JSON.parse(readFileSync(path.join(ws, 'packages/otel-helper/package.json'), 'utf8')).version;
const wsDockerfiles = readdirSync(path.join(ws, 'example')).filter((d) => existsSync(path.join(ws, 'example', d, 'Dockerfile.demo'))).length;
const temps = [];
after(() => { for (const t of temps) rmSync(t, { recursive: true, force: true }); });
const tmp = (prefix) => { const d = mkdtempSync(path.join(tmpdir(), prefix)); temps.push(d); return d; };

/** A copy of only the version-carrying files of the workspace (READMEs included). */
function fixture() {
  const root = tmp('version-mjs-');
  cpSync(path.join(ws, 'package-lock.json'), path.join(root, 'package-lock.json'));
  cpSync(path.join(ws, 'README.md'), path.join(root, 'README.md'));
  cpSync(path.join(ws, 'HOW-TO.md'), path.join(root, 'HOW-TO.md'));
  for (const dir of PACKAGES) {
    mkdirSync(path.join(root, 'packages', dir), { recursive: true });
    for (const f of ['package.json', 'README.md']) cpSync(path.join(ws, 'packages', dir, f), path.join(root, 'packages', dir, f));
  }
  for (const [dir, rel] of Object.entries(VERSION_CONSTANTS)) {
    mkdirSync(path.dirname(path.join(root, 'packages', dir, rel)), { recursive: true });
    cpSync(path.join(ws, 'packages', dir, rel), path.join(root, 'packages', dir, rel));
  }
  for (const ex of ['node-api', 'node-backend']) {
    mkdirSync(path.join(root, 'example', ex), { recursive: true });
    for (const f of ['Dockerfile.demo', 'package.json', 'package-lock.json']) cpSync(path.join(ws, 'example', ex, f), path.join(root, 'example', ex, f));
  }
  return root;
}

/** Six npm-pack-shaped tarballs for `version`; `mutate(dir, manifest)` edits a manifest. */
function tarballs(version, mutate = () => {}) {
  const dir = tmp('tarballs-');
  for (const pkgDir of PACKAGES) {
    const stage = tmp('pack-');
    mkdirSync(path.join(stage, 'package'));
    const manifest = { name: `@staffops/${pkgDir}`, version, ...(pkgDir === 'otel-helper' ? {} : { peerDependencies: { [CORE]: version } }) };
    mutate(pkgDir, manifest);
    writeFileSync(path.join(stage, 'package/package.json'), JSON.stringify(manifest));
    execFileSync('tar', ['-czf', path.join(dir, tarballName(pkgDir, version)), '-C', stage, 'package']);
  }
  return dir;
}

/** A fake npm executable printing FAKE_NPM_STDOUT and exiting with FAKE_NPM_STATUS; records its argv. */
function fakeNpm() {
  const dir = tmp('fake-npm-');
  const bin = path.join(dir, 'npm');
  writeFileSync(bin, `#!${process.execPath}\nrequire('fs').writeFileSync(${JSON.stringify(path.join(dir, 'argv.json'))}, JSON.stringify(process.argv.slice(2)));\nprocess.stdout.write(process.env.FAKE_NPM_STDOUT || '');\nprocess.exit(Number(process.env.FAKE_NPM_STATUS || 0));\n`);
  chmodSync(bin, 0o755);
  return { bin, argv: () => JSON.parse(readFileSync(path.join(dir, 'argv.json'), 'utf8')) };
}

const run = (args, env = {}) => spawnSync(process.execPath, [script, ...args], { encoding: 'utf8', env: { ...process.env, ...env } });

describe('parseVersion / distTag / tarballName', () => {
  it('accepts a version or a nodejs/v tag', () => {
    assert.equal(parseVersion('0.3.0'), '0.3.0');
    assert.equal(parseVersion('nodejs/v0.3.0-rc.1'), '0.3.0-rc.1');
    assert.equal(parseVersion('0.0.0-bootstrap.0'), '0.0.0-bootstrap.0');
  });
  for (const bad of ['v0.3.0', 'go/v0.3.0', '0.3', '0.3.0+build.1', '01.2.3', '', undefined, 'nodejs/v0.3.0 ']) {
    it(`rejects ${JSON.stringify(bad)}`, () => assert.throws(() => parseVersion(bad), /not a release version/));
  }
  it('pre-releases go to "next", releases to "latest"', () => {
    assert.equal(distTag('0.3.0-rc.1'), 'next');
    assert.equal(distTag('0.3.0'), 'latest');
  });
  it('names tarballs as npm pack does', () => assert.equal(tarballName('otel-helper-sql', '1.0.0'), 'staffops-otel-helper-sql-1.0.0.tgz'));
  it('publishes the core first', () => assert.equal(PACKAGES[0], 'otel-helper'));
});

describe('check', () => {
  it('the workspace is consistent at its own version', () => assert.deepEqual(check(ws, current), []));

  it('every package carries a VERSION constant (B14 re-probe L2: -pino and -winston too)', () => {
    assert.deepEqual(Object.keys(VERSION_CONSTANTS).sort(), [...PACKAGES].sort());
  });

  it('a mismatching tag reports every location, exactly', () => {
    const problems = check(ws, '9.9.9');
    // 6 versions + 6 lock versions + 5 peers + 5 lock peers + 6 constants + one per Dockerfile.demo.
    assert.equal(problems.length, 28 + wsDockerfiles, problems.join('\n'));
    assert.ok(problems.every((p) => p.endsWith('want "9.9.9"')));
  });

  it('one drifted peer pin is reported alone', () => {
    const root = fixture();
    const file = path.join(root, 'packages/otel-helper-pino/package.json');
    const pkg = JSON.parse(readFileSync(file, 'utf8'));
    pkg.peerDependencies[CORE] = `^${current}`;
    writeFileSync(file, JSON.stringify(pkg));
    assert.deepEqual(check(root, current), [
      `packages/otel-helper-pino/package.json peerDependencies["${CORE}"]: "^${current}", want "${current}"`,
    ]);
  });

  it('a missing VERSION constant and an unexpected package directory are reported; a stray file is not a package', () => {
    const root = fixture();
    writeFileSync(path.join(root, 'packages/otel-helper/src/version.ts'), 'export const V = 1;\n');
    writeFileSync(path.join(root, 'packages/.DS_Store'), '');
    mkdirSync(path.join(root, 'packages/otel-helper-extra'));
    const problems = check(root, current);
    assert.ok(problems.some((p) => p.startsWith('packages/otel-helper/src/version.ts VERSION: (not found)')), problems.join('\n'));
    const dirSet = problems.filter((p) => p.startsWith('packages/ directory set'));
    assert.equal(dirSet.length, 1);
    assert.ok(!dirSet[0].includes('.DS_Store'));
  });

  it('a broken package.json is reported with its path', () => {
    const root = fixture();
    writeFileSync(path.join(root, 'packages/otel-helper-sql/package.json'), '{');
    assert.throws(() => check(root, current), /otel-helper-sql[/\\]package\.json: invalid JSON/);
  });

  it('README mentions of another version are warnings, not failures', () => {
    const root = fixture();
    assert.deepEqual(docWarnings(root, current), []);
    const w = docWarnings(root, '9.9.9');
    assert.ok(w.length > 0 && w.every((x) => x.includes('(by hand)')), w.join('\n'));
  });

  it('flags a stale release-candidate, install-spec or tag mention as a warning (check still passes)', () => {
    const root = fixture();
    writeFileSync(path.join(root, 'HOW-TO.md'), 'Release candidate `0.0.1-rc.1`; `nodejs/v0.0.1-rc.1`; npm install @staffops/otel-helper-redis@0.0.1-rc.1.\n');
    const w = docWarnings(root, current);
    assert.equal(w.length, 3, w.join('\n'));
    assert.ok(w.every((x) => x.startsWith('HOW-TO.md: mentions "0.0.1-rc.1"') && x.endsWith('(by hand)')), w.join('\n'));
    assert.deepEqual(check(root, current), []);
  });

  it('warns when a stable version is still described as a release candidate', () => {
    const root = fixture();
    set(root, '1.2.3-rc.4');
    assert.ok(!docWarnings(root, '1.2.3-rc.4').some((x) => x.includes('stable release')));
    set(root, '1.2.3');
    const w = docWarnings(root, '1.2.3');
    assert.ok(w.length > 0, 'the shipped READMEs say "Release candidate"');
    assert.ok(w.every((x) => x.includes('says "release candidate" ("1.2.3"), but "1.2.3" is a stable release (by hand)')), w.join('\n'));
    // The version itself was rewritten everywhere: no stale-version warning.
    assert.ok(!w.some((x) => x.includes(': mentions ')), w.join('\n'));
  });
});

describe('set', () => {
  it('moves every location together, READMEs included, then check passes without warnings', () => {
    const root = fixture();
    const changed = set(root, '1.2.3-rc.4');
    assert.ok(changed.length >= 6 + 1 + 4 + 2);
    assert.deepEqual(check(root, '1.2.3-rc.4'), []);
    assert.deepEqual(docWarnings(root, '1.2.3-rc.4'), []);
    assert.match(readFileSync(path.join(root, 'example/node-api/Dockerfile.demo'), 'utf8'), /^ARG HELPER_VERSION=1\.2\.3-rc\.4$/m);
    assert.match(readFileSync(path.join(root, 'README.md'), 'utf8'), /staffops-otel-helper-1\.2\.3-rc\.4\.tgz/);
  });

  it('rewrites only the DOC_RES mentions of a README, nothing else', () => {
    const root = fixture();
    const file = path.join(root, 'packages/otel-helper-pino/README.md');
    writeFileSync(file, `> **Not released yet** (\`${current}\`)\nnpm install ./staffops-otel-helper-pino-${current}.tgz\nChangelog: since ${current} the API is stable.\n`);
    set(root, '4.0.0');
    assert.equal(readFileSync(file, 'utf8'),
      `> **Not released yet** (\`4.0.0\`)\nnpm install ./staffops-otel-helper-pino-4.0.0.tgz\nChangelog: since ${current} the API is stable.\n`);
  });

  it('rewrites every release-candidate mention (rc.N -> rc.N+1), HOW-TO.md included, and keeps other versions', () => {
    const root = fixture();
    const text = (v) => [
      `> **Release candidate \`${v}\`**, the first npm release (tag`,
      `> \`nodejs/v${v}\`); install it with`,
      `> \`npm install @staffops/otel-helper@${v} @staffops/otel-helper-sql@${v} pino\`.`,
      `Prerequisites: the helper (release candidate`,
      `\`${v}\`, dist-tag \`next\`); a bare npm install @staffops/otel-helper-pino@${v}.`,
      'Floor: @staffops/otel-helper@1.0.0 and `nodejs/v1.0.0` and Release candidate `1.0.0-rc.9` stay.',
      '',
    ].join('\n');
    const files = ['README.md', 'HOW-TO.md', 'packages/otel-helper-winston/README.md'].map((f) => path.join(root, f));
    for (const f of files) writeFileSync(f, text(current));
    const next = '7.0.0-rc.2';
    set(root, next);
    for (const f of files) assert.equal(readFileSync(f, 'utf8'), text(next), f);
    assert.deepEqual(docWarnings(root, next).filter((w) => !w.includes('"1.0.0')), [], 'only the fixed floor mentions remain');
  });

  it('is idempotent', () => {
    const root = fixture();
    set(root, '2.0.0');
    const snapshot = (r) => ['package-lock.json', 'README.md', 'packages/otel-helper-sql/package.json', 'packages/otel-helper/src/version.ts', 'example/node-api/Dockerfile.demo']
      .map((f) => readFileSync(path.join(r, f), 'utf8'));
    const first = snapshot(root);
    set(root, '2.0.0');
    assert.deepEqual(snapshot(root), first);
  });

  it('throws, naming the file, when a VERSION constant or the ARG line is gone', () => {
    const root = fixture();
    writeFileSync(path.join(root, 'packages/otel-helper-aws/src/internal/aws-instrumentation.ts'), '// moved\n');
    assert.throws(() => set(root, '2.0.0'), /aws-instrumentation\.ts: no match/);
    const root2 = fixture();
    writeFileSync(path.join(root2, 'example/node-api/Dockerfile.demo'), 'FROM scratch\n');
    assert.throws(() => set(root2, '2.0.0'), /Dockerfile\.demo: no match/);
  });

  it('throws when an opt-in package lost its core peer', () => {
    const root = fixture();
    const file = path.join(root, 'packages/otel-helper-redis/package.json');
    const pkg = JSON.parse(readFileSync(file, 'utf8'));
    delete pkg.peerDependencies[CORE];
    writeFileSync(file, JSON.stringify(pkg));
    assert.throws(() => set(root, '2.0.0'), /otel-helper-redis[/\\]package\.json: no peerDependencies/);
  });
});

describe('check --tarballs', () => {
  it('matching tarballs pass', () => {
    assert.deepEqual(check(fixture(), current, tarballs(current)), []);
  });

  it('a tarball with another name or peer, and a missing one, fail', () => {
    const dir = tarballs(current, (pkgDir, m) => {
      if (pkgDir === 'otel-helper-aws') m.name = '@other/otel-helper-aws';
      if (pkgDir === 'otel-helper-redis') m.peerDependencies[CORE] = '0.0.1';
    });
    rmSync(path.join(dir, tarballName('otel-helper-winston', current)));
    assert.equal(check(fixture(), current, dir).length, 3);
  });

  it('an unreadable tarball is reported with its path', () => {
    const dir = tarballs(current);
    writeFileSync(path.join(dir, tarballName('otel-helper-sql', current)), 'not a tarball');
    assert.throws(() => check(fixture(), current, dir), /staffops-otel-helper-sql-.*\.tgz: cannot read package\/package\.json/);
  });
});

describe('verifySums (JS10 publication; M2: the publish job checks the tarballs build-lib-node recorded)', () => {
  const sums = (dir, version) => {
    const file = path.join(tmp('sums-'), 'SHA256SUMS');
    writeFileSync(file, execFileSync('sha256sum', PACKAGES.map((d) => tarballName(d, version)), { cwd: dir, encoding: 'utf8' }));
    return file;
  };

  it('sha256sum output for the six tarballs verifies', () => {
    const dir = tarballs('1.0.0');
    assert.deepEqual(verifySums(dir, '1.0.0', sums(dir, '1.0.0')), []);
  });

  it('a changed tarball, an extra tarball, a missing line and a junk line fail', () => {
    const dir = tarballs('1.0.0');
    const file = sums(dir, '1.0.0');
    writeFileSync(path.join(dir, tarballName('otel-helper-pino', '1.0.0')), 'tampered');
    writeFileSync(path.join(dir, 'staffops-evil-1.0.0.tgz'), 'x');
    const lines = readFileSync(file, 'utf8').split('\n').filter((l) => !l.includes('winston'));
    writeFileSync(file, `${lines.join('\n')}\nnot a sum line\n`);
    const problems = verifySums(dir, '1.0.0', file);
    assert.equal(problems.length, 4, problems.join('\n'));
    assert.ok(problems.some((p) => p.includes('pino') && p.includes('sha256 differs')));
    assert.ok(problems.some((p) => p.includes('winston') && p.includes('not in')));
    assert.ok(problems.some((p) => p.includes('staffops-evil') && p.includes('unexpected tarball')));
    assert.ok(problems.some((p) => p.includes('not a sha256sum line')));
  });

  it('a listed name outside the six fails', () => {
    const dir = tarballs('1.0.0');
    const file = sums(dir, '1.0.0');
    writeFileSync(file, `${readFileSync(file, 'utf8')}${'0'.repeat(64)}  other.tgz\n`);
    assert.deepEqual(verifySums(dir, '1.0.0', file), ['other.tgz: listed but not one of the six release tarballs']);
  });
});

describe('registryState (JS10 publication; M1: never skip a version published with other bytes)', () => {
  const npm = fakeNpm();
  const tarball = path.join(tarballs('1.0.0'), tarballName('otel-helper', '1.0.0'));
  const state = (stdout, status = 0, extra = {}) => {
    process.env.FAKE_NPM_STDOUT = typeof stdout === 'string' ? stdout : JSON.stringify(stdout);
    process.env.FAKE_NPM_STATUS = String(status);
    try {
      return registryState(tarball, { registry: 'https://registry.example.com/', npmBin: npm.bin, ...extra });
    } finally {
      delete process.env.FAKE_NPM_STDOUT;
      delete process.env.FAKE_NPM_STATUS;
    }
  };

  it('match: same version and sha512 -> same; the query names the scope registry and the userconfig', () => {
    assert.equal(state({ version: '1.0.0', 'dist.integrity': integrity(tarball) }, 0, { userconfig: '/tmp/u' }), 'same');
    assert.deepEqual(npm.argv(), ['view', '@staffops/otel-helper@1.0.0', 'version', 'dist.integrity', '--json',
      '--@staffops:registry=https://registry.example.com/', '--userconfig', '/tmp/u']);
  });
  it('mismatch: other bytes -> different', () => {
    assert.equal(state({ version: '1.0.0', 'dist.integrity': 'sha512-AAAA' }), 'different');
  });
  it('absent: E404 (unknown package or version) and an empty answer', () => {
    assert.equal(state({ error: { code: 'E404', summary: 'No match found for version 1.0.0' } }, 1), 'absent');
    assert.equal(state(''), 'absent');
  });
  it('anything else fails loudly instead of reading as absent', () => {
    assert.throws(() => state({ error: { code: 'E401', summary: 'Unauthorized' } }, 1), /E401: Unauthorized/);
    assert.throws(() => state('npm error network', 1), /unreadable answer/);
    assert.throws(() => state('', 1), /unreadable answer/);
    assert.throws(() => state({ version: '1.0.0' }), /no dist\.integrity/);
    assert.throws(() => state({ version: '0.9.0', 'dist.integrity': integrity(tarball) }), /answered version "0\.9\.0"/);
    assert.throws(() => state({ version: '1.0.0', 'dist.integrity': integrity(tarball) }, 3), /exit 3/);
    assert.throws(() => registryState(tarball, { registry: 'https://r.example.com/', npmBin: '/nonexistent/npm' }), /ENOENT/);
    assert.throws(() => registryState(tarball, { npmBin: npm.bin }), /needs --registry/);
  });
  it('CLI: prints the state, NPM_BIN overrides npm', () => {
    const r = run(['registry-state', tarball, '--registry', 'https://registry.example.com/'], {
      NPM_BIN: npm.bin, FAKE_NPM_STDOUT: JSON.stringify({ version: '1.0.0', 'dist.integrity': 'sha512-BBBB' }),
    });
    assert.equal(r.status, 0, r.stderr);
    assert.equal(r.stdout, 'different\n');
    const e = run(['registry-state', tarball, '--registry', 'https://registry.example.com/'], { NPM_BIN: npm.bin, FAKE_NPM_STDOUT: '{"error":{"code":"E500"}}', FAKE_NPM_STATUS: '1' });
    assert.equal(e.status, 1);
    assert.match(e.stderr, /E500/);
  });
});

describe('CLI', () => {
  it('check exits 0 on a match and 1 on a mismatch', () => {
    const ok = run(['check', `nodejs/v${current}`, '--root', ws]);
    assert.equal(ok.status, 0, ok.stderr);
    const bad = run(['check', 'nodejs/v9.9.9', '--root', ws]);
    assert.equal(bad.status, 1);
    assert.match(bad.stderr, /^MISMATCH /m);
    assert.match(bad.stderr, /^WARN /m);
  });
  it('set, dist-tag and tarballs', () => {
    const root = fixture();
    const s = run(['set', '3.0.0', '--root', root]);
    assert.equal(s.status, 0, s.stderr);
    assert.match(s.stdout, /^set {2}package-lock\.json -> 3\.0\.0$/m);
    assert.equal(run(['dist-tag', 'nodejs/v1.0.0-rc.1']).stdout, 'next\n');
    assert.equal(run(['tarballs', '1.0.0']).stdout.split('\n')[0], 'staffops-otel-helper-1.0.0.tgz');
  });
  it('verify-sums exits 0 on a match, 1 on a mismatch', () => {
    const dir = tarballs('1.0.0');
    const file = path.join(tmp('sums-'), 'S');
    writeFileSync(file, execFileSync('sha256sum', PACKAGES.map((d) => tarballName(d, '1.0.0')), { cwd: dir, encoding: 'utf8' }));
    assert.equal(run(['verify-sums', '1.0.0', '--tarballs', dir, '--sums', file]).status, 0);
    writeFileSync(path.join(dir, tarballName('otel-helper', '1.0.0')), 'x');
    assert.equal(run(['verify-sums', '1.0.0', '--tarballs', dir, '--sums', file]).status, 1);
  });
  it('bad usage exits 2 with the usage line', () => {
    for (const args of [['check', 'v1.0.0'], ['frobnicate', '1.0.0'], [], ['check', '1.0.0', '--root'], ['check', '1.0.0', '--force'],
      ['check', '1.0.0', 'extra'], ['verify-sums', '1.0.0'], ['registry-state'], ['registry-state', 'x.tgz'], ['check', '1.0.0', '--root', '--tarballs']]) {
      const r = run(args);
      assert.equal(r.status, 2, `${args.join(' ')}: ${r.stderr}`);
      assert.match(r.stderr, /^usage: node ci\/version\.mjs /m, args.join(' '));
    }
  });
  it('main() is importable and returns the exit code', () => {
    const out = [];
    assert.equal(main(['dist-tag', '1.0.0'], (l) => out.push(l)), 0);
    assert.deepEqual(out, ['latest']);
  });
});

// B14 carry-over (1): the examples depend on the published helper, locked; `set` leaves that pin alone.
describe('examples (B14 carry-over 1: the published helper, locked)', () => {
  const pinned = examplePin(ws);
  const editJson = (file, edit) => { const v = JSON.parse(readFileSync(file, 'utf8')); edit(v); writeFileSync(file, JSON.stringify(v, null, 2)); };
  const lockOf = (root, ex = 'node-api') => path.join(root, 'example', ex, 'package-lock.json');
  const fail = (root) => check(root, current).filter((p) => p.startsWith('example/'));

  it('the workspace examples pin one exact version, locked from npmjs with a sha512 integrity', () => {
    assert.match(pinned, /^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$/);
    const rows = collectExamples(ws);
    assert.ok(rows.length >= 4 * 5, rows.length);
    for (const [label, found, want] of rows) assert.equal(found, want, label);
    const lock = JSON.parse(readFileSync(lockOf(ws), 'utf8'));
    assert.equal(lock.packages[`node_modules/${CORE}`].resolved, npmjsTarball(pinned));
  });

  it('a range, a drifted lockfile, another registry, a missing integrity and a missing lockfile fail check', () => {
    const range = fixture();
    editJson(path.join(range, 'example/node-backend/package.json'), (p) => { p.dependencies[CORE] = `^${pinned}`; });
    assert.deepEqual(fail(range), [`example/node-backend/package.json dependencies["${CORE}"]: "^${pinned}", want "${pinned}"`]);
    const first = fixture();
    editJson(path.join(first, 'example/node-api/package.json'), (p) => { p.dependencies[CORE] = `~${pinned}`; });
    assert.deepEqual(fail(first), [`example/node-api/package.json dependencies["${CORE}"]: "~${pinned}", want "an exact published version X.Y.Z[-pre]"`],
      'only the example that sets the pin is reported');
    const lock = fixture();
    editJson(lockOf(lock), (l) => {
      l.packages[''].dependencies[CORE] = '0.0.1';
      l.packages[`node_modules/${CORE}`].version = '0.0.1';
      l.packages[`node_modules/${CORE}`].resolved = 'https://npm.pkg.github.com/download/x.tgz';
      delete l.packages[`node_modules/${CORE}`].integrity;
    });
    const p = fail(lock);
    assert.equal(p.length, 5, p.join('\n'));
    assert.ok(p.some((x) => x.includes('resolved') && x.endsWith(`want "${npmjsTarball(pinned)}"`)));
    assert.ok(p.some((x) => x.includes('integrity: (not found), want "sha512-…"')));
    const sha1 = fixture();
    editJson(lockOf(sha1), (l) => { l.packages[`node_modules/${CORE}`].integrity = 'sha1-AAAA'; });
    assert.deepEqual(fail(sha1), [`example/node-api/package-lock.json node_modules/${CORE} integrity: "sha1-AAAA", want "sha512-…"`]);
    const mirror = fixture();
    editJson(lockOf(mirror, 'node-backend'), (l) => { l.packages['node_modules/x-mirror'] = { version: '1.0.0', resolved: 'https://mirror.example.com/x/-/x-1.0.0.tgz' }; });
    assert.deepEqual(fail(mirror), ['example/node-backend/package-lock.json entries resolved outside https://registry.npmjs.org/: "node_modules/x-mirror", want "none"']);
    const nolock = fixture();
    rmSync(lockOf(nolock, 'node-backend'));
    assert.deepEqual(fail(nolock), ['example/node-backend/package-lock.json: "absent", want "present"']);
  });

  it('pinning another version than the release is a warning (by hand), not a failure', () => {
    const root = fixture();
    assert.deepEqual(exampleWarnings(root, pinned), []);
    const w = exampleWarnings(root, '9.9.9');
    assert.equal(w.length, 1);
    assert.match(w[0], /once 9\.9\.9 is on npmjs, run `node ci\/version\.mjs examples 9\.9\.9` \(by hand\)$/);
    const r = run(['check', '9.9.9', '--root', root]);
    assert.match(r.stderr, /^WARN example\/\*\/package\.json pin /m);
  });

  it('set leaves the examples alone (an unpublished version cannot be locked) and says so', () => {
    const root = fixture();
    const before = ['package.json', 'package-lock.json'].map((f) => readFileSync(path.join(root, 'example/node-api', f), 'utf8'));
    const r = run(['set', '9.0.0', '--root', root]);
    assert.equal(r.status, 0, r.stderr);
    assert.deepEqual(['package.json', 'package-lock.json'].map((f) => readFileSync(path.join(root, 'example/node-api', f), 'utf8')), before);
    assert.match(r.stdout, new RegExp(`^note example/\\*/package\\.json keep @staffops/otel-helper@${pinned.replace(/\./g, '\\.')} \\(published\\); once 9\\.0\\.0 is on npmjs`, 'm'));
    assert.deepEqual(check(root, '9.0.0'), []);
  });

  /** A fake npm that does what `npm install <spec> --save-exact --package-lock-only` does to the two files. */
  function fakeInstaller({ status = 0, resolvedHost = 'https://registry.npmjs.org/', published = true, noop = false } = {}) {
    const dir = tmp('fake-npm-install-');
    const bin = path.join(dir, 'npm');
    writeFileSync(bin, `#!${process.execPath}
const fs = require('fs');
fs.appendFileSync(${JSON.stringify(path.join(dir, 'calls'))}, JSON.stringify({ cwd: process.cwd(), argv: process.argv.slice(2) }) + '\\n');
if (process.argv[2] === 'view') {
  if (!${published}) { process.stderr.write('npm error 404\\n'); process.exit(1); }
  process.stdout.write(process.argv[3].split('@').pop() + '\\n'); process.exit(0);
}
if (${status}) { process.stderr.write('npm error 404 Not Found\\n'); process.exit(${status}); }
if (${noop}) process.exit(0);
const version = process.argv[3].split('@').pop();
const pkg = JSON.parse(fs.readFileSync('package.json', 'utf8'));
pkg.dependencies['${CORE}'] = version;
fs.writeFileSync('package.json', JSON.stringify(pkg, null, 2));
const lock = JSON.parse(fs.readFileSync('package-lock.json', 'utf8'));
lock.packages[''].dependencies['${CORE}'] = version;
lock.packages['node_modules/${CORE}'] = { version, resolved: '${resolvedHost}${CORE}/-/otel-helper-' + version + '.tgz', integrity: 'sha512-AAAA' };
fs.writeFileSync('package-lock.json', JSON.stringify(lock, null, 2));
`);
    chmodSync(bin, 0o755);
    const calls = () => readFileSync(path.join(dir, 'calls'), 'utf8').trim().split('\n').map((l) => JSON.parse(l));
    return { bin, calls };
  }

  it('setExamples pins the published version in every example through npm (no scripts, npmjs, lock only), then check passes', () => {
    const root = fixture();
    const npm = fakeInstaller();
    const changed = setExamples(root, '9.9.9', { npmBin: npm.bin });
    assert.equal(changed.length, 4);
    const [view, ...calls] = npm.calls();
    assert.deepEqual(view.argv, ['view', `${CORE}@9.9.9`, 'version', '--registry=https://registry.npmjs.org/', '--@staffops:registry=https://registry.npmjs.org/']);
    assert.deepEqual(calls.map((c) => path.basename(c.cwd)), ['node-api', 'node-backend']);
    assert.deepEqual(calls[0].argv, ['install', `${CORE}@9.9.9`, '--save-exact', '--package-lock-only', '--ignore-scripts', '--no-audit', '--no-fund',
      '--registry=https://registry.npmjs.org/', '--@staffops:registry=https://registry.npmjs.org/']);
    assert.equal(examplePin(root), '9.9.9');
    assert.deepEqual(fail(root), []);
    assert.deepEqual(exampleWarnings(root, '9.9.9'), []);
  });

  it('setExamples fails loudly on an npm error, an npm that changed nothing, or a lock from another registry', () => {
    assert.throws(() => setExamples(fixture(), '9.9.9', { npmBin: fakeInstaller({ status: 1 }).bin }), /in example\/node-api: exit 1: npm error 404 Not Found/);
    assert.throws(() => setExamples(fixture(), '9.9.9', { npmBin: fakeInstaller({ noop: true }).bin }), /examples after npm install: .*want "9\.9\.9"/);
    const untouched = fixture();
    const before = readFileSync(path.join(untouched, 'example/node-api/package.json'), 'utf8');
    assert.throws(() => setExamples(untouched, '9.9.9', { npmBin: fakeInstaller({ published: false }).bin }), /not on https:\/\/registry\.npmjs\.org\/ .*nothing changed/);
    assert.equal(readFileSync(path.join(untouched, 'example/node-api/package.json'), 'utf8'), before);
    assert.throws(() => setExamples(fixture(), '9.9.9', { npmBin: fakeNpm().bin }), /is not on/);
    assert.throws(() => setExamples(fixture(), '9.9.9', { npmBin: fakeInstaller({ resolvedHost: 'https://npm.pkg.github.com/' }).bin }), /resolved: "https:\/\/npm\.pkg\.github\.com\//);
    assert.throws(() => setExamples(fixture(), '9.9.9', { npmBin: '/nonexistent/npm' }), /ENOENT/);
  });

  it('CLI: examples needs a version (exit 2) and runs NPM_BIN', () => {
    const root = fixture();
    const bad = run(['examples', '--root', root]);
    assert.equal(bad.status, 2, bad.stderr);
    const npm = fakeInstaller();
    const r = run(['examples', 'nodejs/v9.9.9', '--root', root], { NPM_BIN: npm.bin });
    assert.equal(r.status, 0, r.stderr);
    assert.match(r.stdout, /^set {2}example\/node-api\/package-lock\.json -> @staffops\/otel-helper@9\.9\.9$/m);
  });
});
