---
name: change-loop
description: Use for any non-trivial change in otel-libs (lib behavior, env-var contract, emitted telemetry, examples, CI, specs). Runs the AGENTS.md change loop — spec first, implement in Docker, adversarial validation by observability + dev agents, code-review, CHANGELOG — with ready-to-paste agent briefs and the standing constraints every agent must follow.
---

# Change loop (otel-libs)

Rules live in [AGENTS.md](../../../AGENTS.md) and [specs/README.md](../../../specs/README.md);
this skill is the procedure. If they disagree, AGENTS.md wins — fix this file.

## 1. Spec first
- Contract change (env vars, precedence, defaults, emitted attributes, metric
  names) → a requirement/decision in `specs/<feature>/` **before** code.
- Existing spec wrong? Record a dated correction in place
  (`> **Corrected YYYY-MM-DD (<task>).** …`) — never edit silently.
- Run `python3 tools/spec_lint.py` (in `python:3.12-slim` for the official run).
- New spec set → adversarial review (step 3) **before** implementing.

## 2. Implement (Docker only)
- Go + Python together (ADR-004); .NET/Node.js lag via `parity/cases.json`
  `xfail:<task>` / `skip:phase-*` — see the `parity-case` skill.
- Tests: `docker compose -f testing/docker-compose.yml run --rm go-tests` /
  `python-tests` (+ `PY_VERSION=… PY_DEPS=lower|latest`), e2e
  `testing/e2e/smoke.sh`. Before a tag or PR: `testing/verify.sh`.
- Diagnostics: Go package stderr logger; Python `otel_helper.*` loggers;
  never print secrets.

## 3. Adversarial validation
Launch **in parallel** (one message): an `observability` agent (spec/semconv/
backend conformance) and a `dev` agent (reproduce, idioms). Prefer the
project `spec-refuter` agent for spec-only reviews. Only **one** agent may use
containers at a time — give the others "read-only, no containers".

Brief template (fill the brackets; keep the constraints block verbatim):

```
Refute <change> in the repo at <abs path> (branch <b>, uncommitted: `git diff`
+ <untracked paths>). Context: <specs/files to read>. Your angle: <angle>.
Check: <specific risks>.
Constraints: <read-only, no containers | Docker only, default compose project
`testing/docker-compose.yml`, run containers one at a time>; stop only services
you started; never prune, `down -v`, or delete volumes/images/files; no
commits; atomic writes to parity/cases.json (temp + rename); no company names
(use <org>/example.com). Every claim needs file:line or command output.
Report BLOCKING/HIGH/MEDIUM/LOW, each with a concrete fix.
```

Iterate: fix → re-review the fixes (same agent via SendMessage keeps
context) until no BLOCKING/HIGH remains. The implementer is never its own
reviewer — say so when it happened.

## 4. Code review
`code-review` agent on the diff (read-only). Ask specifically for parity,
diagnostics channel, secrets, recorded spec corrections, CHANGELOG presence.

## 5. CHANGELOG + docs
- `CHANGELOG.md` → `## [Unreleased]`, under the right language section;
  breaking items also in the migration guide.
- Docs that describe the behavior (root README env table, per-language
  README/HOW-TO); regenerate `docs/site/index.md` with
  `python3 tools/regen_index.py`.
- Commit per validated task (Conventional Commits with scope, e.g.
  `fix(go): …`); push only when the owner asks.

## Disk hygiene (shared Docker daemon)
Check `docker system df` before long runs. Remove only what this session
built, after confirming; the guard hook asks before any prune/rm/down -v.
