# Handoff

Where the work stands and how to pick it up. Update this page whenever the
state below changes.

**Last updated:** 2026-11-23.
**Audience:** whoever continues the work, human or agent.
**Branch:** `feat/v0.3-config-contract`. The specs are linked on GitHub,
branch `main`; those links resolve after the branch is merged to main. Until
then, open the repo-relative path given next to each link.

## State of the 0.3 release

| Language | State | Published |
|----------|-------|-----------|
| Go | 0.3 contract implemented (Phase A) | release candidate tags `go/v0.3.0-rc.2`, `go/ext/<module>/v0.3.0-rc.2` |
| Python | 0.3 contract implemented (Phase A) | release candidate `v0.3.0-rc.2` (still under the old package name); the renamed package is unreleased |
| Node.js | 0.3 contract implemented (Phase B) | release candidate `0.3.0-rc.1`, npm dist-tag `next` |
| .NET | 0.3 contract implemented (Phase C, tasks C4–C19) | release candidate `0.3.0-rc.2`; stable line `0.2.1` |

The 0.3 branch is in the **soak**: Phase K on the k8s01 test cluster.
Until the soak report (task K11), the language trees `go/`, `python/`,
`nodejs/` and `dotnet/` are frozen, including their Markdown files.

## Uncommitted work

- **A50 documentation pass, phase 1** (2026-11-23): written, pending
  validation and commit. It covers:
  - `mkdocs.yml`, `.gitignore`, `.github/workflows/docs.yml`;
  - the new pages under `docs/site/library/`, `docs/site/observe/`,
    `docs/site/dashboards/`, `docs/site/platform/`, `docs/site/contributing/`;
  - the six symlinks in `docs/site/nodejs/packages/`, and this file with its
    symlink `docs/site/handoff.md`;
  - `dashboards/demo/` (the old `dashboards/01-03` deleted);
  - `README.md`, `docs/site/index.md`, `AGENTS.md`, `CHANGELOG.md`;
  - the task notes in `specs/config-contract-v0.3/tasks.md` and
    `specs/dotnet-otel-helper/design.md`.
- **Phase 2** waits for the soak:
  - the edits inside the language trees;
  - the rewrite of the custom-signals example apps;
  - the cluster measurements marked "to be measured (phase 2)";
  - the screenshots named by the "Screenshot (phase 2)" notes.

  The full list is on the A50 row.

## Open work

Each item is checked against its task table on 2026-11-23.

### Contract

[tasks](https://github.com/StaffOps/otel-libs/blob/main/specs/config-contract-v0.3/tasks.md) —
`specs/config-contract-v0.3/tasks.md`

| Task | Status |
|------|--------|
| A12 | Collector and platform prerequisites, external; blocks the 0.3.0 rollout, not the rc |
| A15 | Owner action |
| A42 | .NET, parked |
| A43 | Go and .NET DB exemplars; the .NET part is parked |
| A47 | MongoDB, backlog |
| A50 | Documentation; phase 1 written, phase 2 after K11 |
| A51 | gRPC 3xx helper line |
| A52 | Redis exception detail; scope not decided |
| A53 | AWS exception detail |

### .NET

[tasks](https://github.com/StaffOps/otel-libs/blob/main/specs/dotnet-otel-helper/tasks.md) —
`specs/dotnet-otel-helper/tasks.md`

| Task | Status |
|------|--------|
| C0 | `net10.0` only; the last step |
| C20, C21 | HTTP and gRPC exception detail; parked |
| C22 | parity driver; parked |

### Soak

[tasks](https://github.com/StaffOps/otel-libs/blob/main/specs/soak-simulation/tasks.md) —
`specs/soak-simulation/tasks.md`, r6.2 table

| Task | Status |
|------|--------|
| K0f | ☐ re-check of the K0e corrections |
| K1 | ◐ platform snapshot built, uncommitted at the time of its note |
| K8a | the 30 h passive window, running as run `k8a-20261123a` (`testing/k8s/soak/runs/k8a-20261123a/`: loop started 2026-11-23T19:16Z, window start 19:26:38Z after the hook roll, warm-up to 2026-11-24T07:26Z, judged span to 2026-11-25T01:26Z; demo image tag `7080d19`, code at commit `0c4c572`, local tag `soak/k8a-20261123a`); the row still shows ☐ |
| K-cal | ☐ calibration |
| K8b | ☐ loaded window |
| K10 | ☐ failure modes |
| K11 | ☐ report and refutation |

### Demo

[tasks](https://github.com/StaffOps/otel-libs/blob/main/specs/demo-environment/tasks.md) —
`specs/demo-environment/tasks.md`

| Task | Status |
|------|--------|
| DE1 | ◐ shared contract; delivered, status note 2026-11-19 |
| DE8 | ◐ gap filing |
| DE9 | ◐ one human click per Grafana link still open |
| DE10 | ☐ optional local path |
| DE11 | ☐ refutation of the demo report |
| DE12 | ☐ examples replaced by the demo apps |
| DE12a | ◐ failure-helper migration of the demo apps; its note says uncommitted and not yet deployed, 2026-11-17 |

## Decisions waiting for the owner

Each item says where it is recorded.

1. **A52 Redis scope:** whether Go, Python and Node.js apply the A36 rule
   to Redis, given that .NET StackExchange.Redis records no text (C14b).
   Recorded on the A52 row: "the owner has not decided the Redis scope".
2. **A15:** claim the Python package name on PyPI as a placeholder. Owner
   action on the A15 row.
3. **Unpark the .NET backlog:** A42, and C20–C22. All are marked parked on
   their rows.
4. **Soak, guard (f) during the back-pressure flip:** whether guard (f)
   stays active while the requested back-pressure flip of K10 runs. The
   soak tasks record that a mid-window platform roll invalidates the
   window "unless it is the requested back-pressure flip" (r6 ruling 5). The
   guard behavior during that flip is not ruled there.
5. **Soak, CI:** whether the soak tests (`testing/k8s/soak/`,
   `testing/k8s/demo/`) are added to CI. Not recorded in the soak tasks.
6. **Push the tag `soak/k8a-20261123a`:** a local tag (ruling K0e-4: "the
   owner pushes tags"). It is not pushed: `git ls-remote --tags origin`
   returned no `soak/` tag on 2026-11-23.
7. **A50 phase 2 (h):** the "affected release `0.3.0rc3`" wording for
   Python (`python/README.md:31` and the gaps row "Python with
   `OTEL_PROPAGATORS=none`"). No `v0.3.0-rc.3` tag exists. Was rc3
   published, and if so how?
8. **DE12 and DE12a:**
   - when DE12 (examples replaced by the demo apps) runs, after the soak;
   - whether the DE12a migration of the demo apps is committed and deployed
     before or after the soak, given the code freeze.

## How to resume

1. Read [AGENTS.md](https://github.com/StaffOps/otel-libs/blob/main/AGENTS.md)
   (`AGENTS.md`: principles, change loop) and
   [specs/README.md](https://github.com/StaffOps/otel-libs/blob/main/specs/README.md)
   (`specs/README.md`: reading order).
2. `testing/verify.sh` runs the CI risk matrix locally, in Docker.
3. Two checks:
   - `python3 tools/spec_lint.py` checks the specs;
   - `python3 -I tools/regen_index.py --check` checks that the site home
     page matches the README.
4. The documentation site builds with `mkdocs build --strict`
   (`mkdocs-material==9.5.39`, the version CI pins).
5. Cluster work happens in namespace `dev-otel-libs` on k8s01, with the
   scripts in `testing/k8s/`.
