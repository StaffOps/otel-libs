---
name: release
description: Use when cutting a release or release candidate of otel-libs (Go, Python, .NET) — version bumps, per-language tags, GitHub Release, verification before and after tagging. Encodes the pitfalls found cutting 0.3.0-rc.1/rc.2.
---

# Release procedure (otel-libs)

Decision record: `specs/config-contract-v0.3/design.md` D10 (+ "A11 ruling").

## Tag scheme (per language — versions diverge, ADR-004)
| Language | Tag | Published by |
|---|---|---|
| Python | `vX.Y.Z[-rc.N]` | CI `publish-python-release` → GitHub Release (wheel + sdist) |
| Go core | `go/vX.Y.Z[-rc.N]` | nothing (module proxy reads the tag) |
| Go ext | `go/ext/{otelaws,otelredis,otelsql}/vX.Y.Z[-rc.N]` | same |
| .NET | `dotnet/vX.Y.Z[-rc.N]` | CI `publish-dotnet-release` → GitHub NuGet |

A `v*` tag never publishes .NET.

## Before tagging
1. All tasks for the release done and validated (change-loop); CHANGELOG
   section states the version and date; migration guide current.
2. Versions: `python/pyproject.toml` in **PEP 440** (`0.3.0rc2`, not
   `0.3.0-rc.2`); Go needs no file change; .NET takes the version from the tag.
3. Docs/consumption pages (`CONSUMING.md`, README matrix) name the new tags.
4. `testing/verify.sh` green — it covers the CI risk corners that local runs
   used to skip (Python 3.13 + lower deps broke rc.1).
5. Commit, push the branch, open/update the PR to `main` and wait for its CI
   (the CI push trigger covers only `main` and tags — a pushed feature branch
   alone runs nothing).

## Tagging (the owner pushes tags; auto mode blocks it for the agent)
- Annotated tags on the exact commit.
- **Never push more than 3 tags in one `git push`** — GitHub then creates no
  push events and no workflow runs (rc.1 lost its CI this way). Push the
  Python tag alone, then the Go tags.
- Give the owner the commands as `! cd <repo> && …` lines.

## After tagging
- `gh run list` → watch the tag run (`gh run watch <id> --exit-status`).
- `gh release view vX.Y.Z… --json isPrerelease,assets`: pre-release tags must
  be **Pre-release**, not Latest (the job passes `--prerelease
  --latest=false` for tags containing `-`; check anyway).
- If the run fails: fix, bump to the next rc (do not move a published tag),
  record the failed rc in the CHANGELOG.

## Final (non-rc) release
Requires the rc soak (`specs/soak-simulation/`) acceptance and D11
prerequisites tracked (A12). Rename the CHANGELOG `[Unreleased]` content to
`[X.Y.Z] - YYYY-MM-DD` and open a fresh `[Unreleased]`.
