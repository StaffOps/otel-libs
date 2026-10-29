// Pack-content check: for every workspace package, `npm pack --dry-run --json`
// must contain each file its `exports` map points at, plus LICENSE and
// package.json, and nothing from src/ or test/. Run from nodejs/ after a build:
//   node ci/check-pack.mjs
import { execFileSync } from 'node:child_process';
import { readdirSync, readFileSync } from 'node:fs';
import path from 'node:path';

// Every target string in an exports map (nested conditions included).
function targets(node, out = new Set()) {
  if (typeof node === 'string') out.add(node.replace(/^\.\//, ''));
  else if (node && typeof node === 'object') for (const v of Object.values(node)) targets(v, out);
  return out;
}

let failed = 0;
for (const dir of readdirSync('packages').sort()) {
  const pkgDir = path.join('packages', dir);
  const pkg = JSON.parse(readFileSync(path.join(pkgDir, 'package.json'), 'utf8'));
  const [report] = JSON.parse(execFileSync('npm', ['pack', '--dry-run', '--json', '--loglevel=error'], { cwd: pkgDir, encoding: 'utf8' }));
  const files = new Set(report.files.map((f) => f.path));
  const required = new Set([...targets(pkg.exports), 'LICENSE', 'package.json']);
  const missing = [...required].filter((f) => !files.has(f));
  const unwanted = [...files].filter((f) => /^(src|test)\//.test(f));
  if (missing.length || unwanted.length) {
    failed++;
    console.error(`FAIL ${pkg.name}: missing [${missing.join(', ')}] unwanted [${unwanted.join(', ')}]`);
  } else {
    console.log(`ok   ${pkg.name}: ${files.size} files, all ${required.size} export targets present`);
  }
}
process.exit(failed ? 1 : 0);
