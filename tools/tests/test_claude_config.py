"""Tests for the Claude Code project config: hooks, skills and the
spec-refuter agent (requirement ids are cited in each test class). Stdlib unittest only:

    python -m unittest discover -s tools/tests

The hook tests pipe the JSON Claude Code sends on stdin into each script and
need `sh`, `jq` and (for the session hook) `git`. They are skipped when a tool
is missing, unless REQUIRE_HOOK_TESTS=1 is set (CI spec-checks and
testing/verify.sh set it): then a missing tool is a failure, so the hook tests
can never pass by not running.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOOKS = REPO / ".claude" / "hooks"
REQUIRE_HOOK_TESTS = os.environ.get("REQUIRE_HOOK_TESTS") == "1"


def needs(*tools: str):
    """Skip when a tool is missing, or fail when REQUIRE_HOOK_TESTS=1."""
    missing = [t for t in tools if shutil.which(t) is None]

    def wrap(cls):
        if not missing:
            return cls
        reason = f"{'/'.join(missing)} not installed"
        if not REQUIRE_HOOK_TESTS:
            return unittest.skip(reason)(cls)

        def fail(self):
            self.fail(f"REQUIRE_HOOK_TESTS=1 but {reason}")

        cls.setUp = fail
        return cls

    return wrap


def run_hook(script: str, payload: dict) -> dict | None:
    proc = subprocess.run(
        ["sh", str(HOOKS / script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(REPO)},
        timeout=20,
        check=True,
    )
    out = proc.stdout.strip()
    return json.loads(out) if out else None


def bash(cmd: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


@needs("sh", "jq")
class TestCommitReminderRT1(unittest.TestCase):
    """RT1: the commit reminder fires on git commit only."""

    def test_fires_on_git_commit(self):
        for cmd in ("git commit -m x", "cd /a && git add -A && git commit -q -F -", "git -C x commit -m y",
                    "(git commit -m x)", "/usr/bin/git commit -m x"):
            out = run_hook("commit-reminder.sh", bash(cmd))
            self.assertIsNotNone(out, cmd)
            self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PreToolUse")

    def test_silent_otherwise(self):
        for cmd in ("docker run golang", "git log --oneline", "grep commit file", "git -C x log", "true",
                    'echo "git commit"'):
            self.assertIsNone(run_hook("commit-reminder.sh", bash(cmd)), cmd)


@needs("sh", "jq")
class TestGuardRT2(unittest.TestCase):
    """RT2: the guard asks before destructive commands and never denies."""

    ASK = (
        "docker builder prune -f",
        "docker system prune -af",
        "docker volume rm x",
        "docker rmi img",
        "docker compose -f a.yml down -v",
        "git push origin :refs/tags/v1",
        "git push --force origin main",
        "git tag -d v1",
        "git reset --hard HEAD",
        "git filter-repo --replace-text x",
        "docker compose down -v",
        "docker compose down --volumes",
        "docker-compose down -v",
        "docker compose down -v --remove-orphans",
        "docker compose down --rmi all",
        "docker compose down \\\n  -v",
        "git push -f origin main",
        "git push -d origin feat",
        "git push origin +main",
        "git -C . push --force",
        "git push --mirror",
        "git tag -fd v1",
        "/usr/local/bin/docker system prune",
        "sudo docker volume rm x",
        "docker -H tcp://x system prune",
        "echo \"it's ok\" && docker volume rm x 'y'",
        "git clean -fdx",
        "git branch -D old",
    )
    ALLOW = (
        "docker compose -f a.yml down",
        "git push -q origin feat",
        "git push origin v0.3.0-rc.2",
        "grep prune x",
        'git commit -m "docker system prune"',
        "docker images",
        "docker volume ls -qf dangling=true",
        "git push -u origin feat/x",
        "git push origin HEAD:main",
        "git push && docker compose up -d --force-recreate",
        "git push origin feat && grep -n :foo f",
        "git tag -a v1 -m x",
        "git clean -n",
    )

    def test_asks_for_destructive(self):
        for cmd in self.ASK:
            out = run_hook("guard-destructive.sh", bash(cmd))
            self.assertIsNotNone(out, cmd)
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask", cmd)

    def test_never_denies_and_ignores_safe(self):
        for cmd in self.ALLOW:
            self.assertIsNone(run_hook("guard-destructive.sh", bash(cmd)), cmd)


@needs("sh", "jq", "git")
class TestSessionContextRT3(unittest.TestCase):
    """RT3: the session hook lists active specs only, within 60 lines."""

    def test_lists_active_specs_only(self):
        out = run_hook("session-context.sh", {})
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("otel-libs session context", ctx)
        self.assertNotIn("go-otel-helper:", ctx)  # shipped (historical) spec
        self.assertLessEqual(len(ctx.splitlines()), 60)


class TestSettingsWiring(unittest.TestCase):
    """RT1, RT2, RT3: the hooks are wired in .claude/settings.json."""

    def test_hooks_wired(self):
        settings = json.loads((REPO / ".claude" / "settings.json").read_text())
        cmds = [h["command"] for e in settings["hooks"]["PreToolUse"] for h in e["hooks"]]
        self.assertTrue(any("guard-destructive.sh" in c for c in cmds))
        self.assertTrue(any("commit-reminder.sh" in c for c in cmds))
        start = [h["command"] for e in settings["hooks"]["SessionStart"] for h in e["hooks"]]
        self.assertTrue(any("session-context.sh" in c for c in start))


def frontmatter(path: Path) -> dict[str, str]:
    lines = path.read_text().splitlines()
    assert lines[0] == "---", path
    end = lines.index("---", 1)
    fm = {}
    for line in lines[1:end]:
        key, _, value = line.partition(":")
        fm[key.strip()] = value.strip()
    return fm


class TestSkillsRT8(unittest.TestCase):
    """RT8: project skills exist with frontmatter and valid relative links."""

    def test_skills_exist_with_frontmatter_and_valid_links(self):
        for name in ("change-loop", "release", "parity-case"):
            path = REPO / ".claude" / "skills" / name / "SKILL.md"
            fm = frontmatter(path)
            self.assertEqual(fm["name"], name)
            self.assertTrue(fm["description"])
            # T-D5: skills reference existing files only.
            for target in __import__("re").findall(r"\]\(((?:\.\./)+[^)#]+)", path.read_text()):
                self.assertTrue((path.parent / target).resolve().exists(), f"{path}: {target}")


class TestRefuterAgentRT9(unittest.TestCase):
    """RT9: the spec-refuter agent is read-only and never prunes."""

    def test_agent_is_read_only(self):
        path = REPO / ".claude" / "agents" / "spec-refuter.md"
        fm = frontmatter(path)
        self.assertEqual(fm["name"], "spec-refuter")
        tools = {t.strip() for t in fm["tools"].split(",")}
        self.assertFalse(tools & {"Edit", "Write", "NotebookEdit"})
        self.assertIn("Never prune", path.read_text())


if __name__ == "__main__":
    unittest.main()
