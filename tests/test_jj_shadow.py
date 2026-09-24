"""Integration tests for the jj shadow and the marker the git shadow honors.

`jj git fetch` and `jj git push` shell out to `git`, which in a co-located
repository reaches the git shadow. These tests run jj through its shadow against
a local bare remote, so a push or fetch the git shadow refuses fails here.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SHADOWS = Path(__file__).resolve().parent.parent / "shadows"
GIT_SHADOW = SHADOWS / "git"
JJ_SHADOW = SHADOWS / "jj"
MARKER = "AGENT_COMMAND_GUARDS_JJ_PID"


def find_real(name: str) -> Path | None:
    """Find `name` on PATH, skipping this repository's shadows."""
    entries = [
        entry
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if entry and Path(entry).resolve() != SHADOWS.resolve()
    ]
    found = shutil.which(name, path=os.pathsep.join(entries))
    return Path(found) if found else None


@unittest.skipIf(os.name == "nt", "the jj and git shadows are bash wrappers")
class JjShadowIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        jj = find_real("jj")
        git = find_real("git")
        if jj is None or git is None:
            self.skipTest("jj and git are required for jj shadow integration tests")
        self.jj = jj
        self.git = git
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.environment = {
            key: value for key, value in os.environ.items() if key != MARKER
        }
        self.environment["PATH"] = os.pathsep.join(
            [str(SHADOWS), str(jj.parent), str(git.parent), "/usr/bin", "/bin"]
        )
        # jj refuses to push a commit without an author, and a host account may
        # have no jj user configured.
        self.environment["JJ_USER"] = "Test User"
        self.environment["JJ_EMAIL"] = "test@example.com"

        self.remote = self.tmp / "remote.git"
        self.run_real_git("init", "-q", "--bare", str(self.remote))
        self.repo = self.tmp / "repo"
        self.run_real_jj("git", "init", "--colocate", str(self.repo), cwd=self.tmp)
        self.run_real_jj("git", "remote", "add", "origin", str(self.remote))
        (self.repo / "tracked.txt").write_text("initial\n")
        self.run_real_jj("commit", "-m", "initial")
        self.run_real_jj("bookmark", "create", "main", "-r", "@-")

    def tearDown(self) -> None:
        if hasattr(self, "_tmp"):
            self._tmp.cleanup()

    def run_real_jj(
        self, *args: str, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [str(self.jj), *args],
            capture_output=True,
            check=False,
            cwd=cwd or self.repo,
            env=self.environment,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def run_real_git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.git), *args],
            capture_output=True,
            check=True,
            cwd=self.tmp,
            text=True,
        )

    def run_jj_shadow(
        self, *args: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(JJ_SHADOW), *args],
            capture_output=True,
            check=False,
            cwd=self.repo,
            env=env or self.environment,
            text=True,
            timeout=60,
        )

    def run_git_shadow(
        self, *args: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(GIT_SHADOW), *args],
            capture_output=True,
            check=False,
            cwd=self.repo,
            env=env or self.environment,
            text=True,
            timeout=60,
        )

    def main_commit(self) -> str:
        return self.run_real_jj(
            "log", "--no-graph", "-r", "main", "-T", "commit_id"
        ).stdout.strip()

    def remote_main(self) -> str:
        return self.run_real_git(
            "--git-dir", str(self.remote), "rev-parse", "refs/heads/main"
        ).stdout.strip()

    def test_jj_git_push_reaches_the_remote(self) -> None:
        result = self.run_jj_shadow("git", "push", "--bookmark", "main")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("git shadow:", result.stderr)
        self.assertEqual(self.remote_main(), self.main_commit())

    def test_jj_git_fetch_runs(self) -> None:
        self.assertEqual(
            self.run_jj_shadow("git", "push", "--bookmark", "main").returncode, 0
        )

        result = self.run_jj_shadow("git", "fetch")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("git shadow:", result.stderr)

    def test_alias_that_hides_the_git_subcommand_is_marked(self) -> None:
        # jj ignores aliases given with --config, so this one lives in repo config.
        self.run_real_jj("config", "set", "--repo", "aliases.up", '["git", "push"]')

        result = self.run_jj_shadow("up", "--bookmark", "main")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.remote_main(), self.main_commit())

    def test_global_options_before_the_subcommand_are_skipped(self) -> None:
        result = self.run_jj_shadow(
            "-R", str(self.repo), "--color", "never", "git", "push", "--bookmark", "main"
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.remote_main(), self.main_commit())

    def test_util_exec_git_is_still_guarded(self) -> None:
        result = self.run_jj_shadow("util", "exec", "--", "git", "stash", "list")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing unsupported `git stash`", result.stderr)

    def test_util_exec_does_not_inherit_an_outer_marker(self) -> None:
        # An outer jj's marker names that jj, never the inner one, but the
        # shadow drops it anyway so `util exec` carries no marker at all.
        env = dict(self.environment)
        env[MARKER] = str(os.getpid())
        result = self.run_jj_shadow(
            "util", "exec", "--", "/usr/bin/env", env=env
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(MARKER, result.stdout)

    def test_direct_git_push_is_still_refused(self) -> None:
        result = self.run_git_shadow("push", "origin", "main")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing unsupported `git push`", result.stderr)
        self.assertNotEqual(
            subprocess.run(
                [str(self.git), "--git-dir", str(self.remote), "rev-parse",
                 "--verify", "--quiet", "refs/heads/main"],
                capture_output=True,
                check=False,
            ).returncode,
            0,
        )

    def test_marker_naming_the_parent_passes_through(self) -> None:
        env = dict(self.environment)
        env[MARKER] = str(os.getpid())

        result = self.run_git_shadow("stash", "list", env=env)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("git shadow:", result.stderr)

    def test_marker_naming_another_process_is_refused(self) -> None:
        env = dict(self.environment)
        env[MARKER] = str(os.getppid())

        result = self.run_git_shadow("stash", "list", env=env)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing unsupported `git stash`", result.stderr)


@unittest.skipIf(os.name == "nt", "the jj shadow is a bash wrapper")
class JjShadowLookupTest(unittest.TestCase):
    def setUp(self) -> None:
        jj = find_real("jj")
        if jj is None:
            self.skipTest("jj is required for jj shadow lookup tests")
        self.jj = jj
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_with_path(self, *entries: Path) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["PATH"] = os.pathsep.join([*(str(entry) for entry in entries), "/usr/bin", "/bin"])
        return subprocess.run(
            [str(JJ_SHADOW), "--version"],
            capture_output=True,
            check=False,
            env=env,
            text=True,
            timeout=30,
        )

    def test_skips_itself_and_symlinks_to_itself(self) -> None:
        linked = self.tmp / "linked"
        linked.mkdir()
        (linked / "jj").symlink_to(JJ_SHADOW)

        result = self.run_with_path(SHADOWS, linked, SHADOWS, self.jj.parent)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("jj "), result.stdout)

    def test_missing_real_jj_fails_loudly(self) -> None:
        result = self.run_with_path(SHADOWS)

        self.assertEqual(result.returncode, 127)
        self.assertIn("could not find the real jj", result.stderr)


if __name__ == "__main__":
    unittest.main()
