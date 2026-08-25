"""Integration tests for Git-to-Jujutsu command translation."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SHADOWS = Path(__file__).resolve().parent.parent / "shadows"
GIT_SHADOW = SHADOWS / "git"


@unittest.skipIf(os.name == "nt", "the git shadow is a bash wrapper")
class GitShadowIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        jj = shutil.which("jj")
        if jj is None:
            self.skipTest("jj is required for Git shadow integration tests")
        self.jj = Path(jj)
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = self.tmp / "repo"
        self.environment = dict(os.environ)
        self.environment["PATH"] = (
            f"{SHADOWS}:{self.jj.parent}:/usr/local/bin:/usr/bin:/bin"
        )
        self.run_jj("git", "init", "--colocate", str(self.repo), cwd=self.tmp)
        (self.repo / "tracked.txt").write_text("initial\n")
        (self.repo / ".gitignore").write_text("*.secret\n")
        self.run_jj("-R", str(self.repo), "commit", "-m", "initial")

    def tearDown(self) -> None:
        if hasattr(self, "_tmp"):
            self._tmp.cleanup()

    def run_jj(
        self, *args: str, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [str(self.jj), *args],
            capture_output=True,
            check=False,
            cwd=cwd,
            env=self.environment,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def run_shadow(
        self, *args: str, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(GIT_SHADOW), *args],
            capture_output=True,
            check=False,
            cwd=cwd or self.repo,
            env=self.environment,
            text=True,
        )

    def test_read_command_after_global_option_uses_jj_repository(self) -> None:
        result = self.run_shadow("--no-pager", "status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Not currently on any branch", result.stdout)
        self.assertEqual(
            result.stderr,
            "Warning: that this repo is managed by jujutsu. `git status` is "
            "supported through a compatibility layer, but `git` commands in general "
            "are not supported; please use `jj` instead.\n",
        )

    def test_compatibility_warning_names_log_subcommand(self) -> None:
        result = self.run_shadow("log", "--oneline")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "Warning: that this repo is managed by jujutsu. `git log` is supported ",
            result.stderr,
        )

    def test_fallback_warning_names_delegated_subcommand(self) -> None:
        colocated = self.tmp / "colocated-warning"
        self.run_jj("git", "init", "--colocate", str(colocated), cwd=self.tmp)

        result = self.run_shadow("rev-parse", "--show-toplevel", cwd=colocated)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "Warning: that this repo is managed by jujutsu. `git rev-parse` is "
            "supported through a compatibility layer",
            result.stderr,
        )
        self.assertNotIn("Note that this project uses jujutsu", result.stderr)

    def test_dash_c_finds_jj_repository(self) -> None:
        result = self.run_shadow("-C", str(self.repo), "status", cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Not currently on any branch", result.stdout)

    def test_jj_status_preserves_git_porcelain(self) -> None:
        (self.repo / "tracked.txt").write_text("modified\n")
        result = self.run_shadow("status", "--short")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, " M tracked.txt\n")

    def test_jj_status_hides_jj_plumbing_without_global_ignores(self) -> None:
        # The co-located layout must produce clean porcelain without relying
        # on the user's global Git ignores.
        environment = dict(self.environment)
        environment["GIT_CONFIG_GLOBAL"] = "/dev/null"
        (self.repo / "tracked.txt").write_text("modified\n")
        result = subprocess.run(
            [str(GIT_SHADOW), "status", "--short"],
            capture_output=True,
            check=False,
            cwd=self.repo,
            env=environment,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, " M tracked.txt\n")

    def test_colocated_jj_status_preserves_git_porcelain(self) -> None:
        colocated = self.tmp / "colocated"
        self.run_jj("git", "init", "--colocate", str(colocated), cwd=self.tmp)
        (colocated / "tracked.txt").write_text("initial\n")
        self.run_jj("-R", str(colocated), "commit", "-m", "initial")
        (colocated / "tracked.txt").write_text("modified\n")

        result = self.run_shadow("status", "--short", cwd=colocated)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, " M tracked.txt\n")

    def test_jj_diff_reads_working_tree(self) -> None:
        (self.repo / "tracked.txt").write_text("modified\n")
        result = self.run_shadow("diff", "HEAD")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("-initial", result.stdout)
        self.assertIn("+modified", result.stdout)

    def test_fresh_colocated_repo_leaves_git_head_alone(self) -> None:
        # With only the virtual root as parent, jj keeps Git's unborn HEAD.
        # The wrapper must not manufacture a replacement.
        fresh = self.tmp / "fresh-colocated"
        self.run_jj("git", "init", "--colocate", str(fresh), cwd=self.tmp)
        head_before = (fresh / ".git" / "HEAD").read_text()

        result = self.run_shadow("status", cwd=fresh)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No commits yet", result.stdout)
        self.assertNotIn("local/jj-shadow-head", result.stdout)
        self.assertEqual((fresh / ".git" / "HEAD").read_text(), head_before)

    def test_read_command_retires_synthetic_head_refs(self) -> None:
        # Repositories served by older shadows carry one or both synthetic
        # branches. A read command must retire them before delegating to Git.
        store = self.repo / ".git"
        parent = self.run_jj(
            "-R", str(self.repo), "log", "-r", "@-", "--no-graph", "-T", "commit_id"
        ).stdout.strip()
        system_git = "/usr/bin/git"
        for bookmark in ("jj-head", "local/jj-shadow-head"):
            subprocess.run(
                [
                    system_git,
                    "--git-dir",
                    str(store),
                    "update-ref",
                    f"refs/heads/{bookmark}",
                    parent,
                ],
                check=True,
                capture_output=True,
                text=True,
            )

        result = self.run_shadow("status", "--short")

        self.assertEqual(result.returncode, 0, result.stderr)
        refs = subprocess.run(
            [system_git, "--git-dir", str(store), "for-each-ref", "refs/heads/"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertNotIn("refs/heads/jj-head", refs)
        self.assertNotIn("refs/heads/local/jj-shadow-head", refs)
        self.assertEqual((store / "HEAD").read_text().strip(), parent)

    def test_colocated_read_command_creates_no_jj_bookmark(self) -> None:
        colocated = self.tmp / "colocated-no-bookmark"
        self.run_jj("git", "init", "--colocate", str(colocated), cwd=self.tmp)
        (colocated / "tracked.txt").write_text("initial\n")
        self.run_jj("-R", str(colocated), "commit", "-m", "initial")

        result = self.run_shadow("status", "--short", cwd=colocated)

        self.assertEqual(result.returncode, 0, result.stderr)
        bookmarks = self.run_jj(
            "-R", str(colocated), "bookmark", "list", "--all"
        ).stdout
        self.assertNotIn("jj-shadow-head", bookmarks)
        self.assertFalse((colocated / ".git" / "HEAD").read_text().startswith("ref:"))

    def test_worktree_remove_refuses_dirty_workspace_without_force(self) -> None:
        workspace = self.tmp / "dirty-workspace"
        self.run_jj("-R", str(self.repo), "workspace", "add", str(workspace))
        (workspace / "changed.txt").write_text("not disposable\n")

        result = self.run_shadow("worktree", "remove", str(workspace))

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(workspace.is_dir())
        self.assertIn("has changes", result.stderr)

    def test_worktree_remove_refuses_ignored_files_without_force(self) -> None:
        workspace = self.tmp / "ignored-workspace"
        self.run_jj("-R", str(self.repo), "workspace", "add", str(workspace))
        secret = workspace / "credentials.secret"
        secret.write_text("not disposable\n")

        result = self.run_shadow("worktree", "remove", str(workspace))

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(secret.is_file())
        self.assertIn("untracked or ignored files", result.stderr)

    def test_worktree_remove_requires_exact_registered_path(self) -> None:
        workspace = self.tmp / "registered" / "same-name"
        workspace.parent.mkdir()
        self.run_jj("-R", str(self.repo), "workspace", "add", str(workspace))
        wrong_path = self.tmp / "unregistered" / "same-name"
        wrong_path.mkdir(parents=True)
        sentinel = wrong_path / "keep.txt"
        sentinel.write_text("keep\n")

        result = self.run_shadow("worktree", "remove", "-f", str(wrong_path))

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(sentinel.is_file())
        workspaces = self.run_jj("-R", str(self.repo), "workspace", "list").stdout
        self.assertIn("same-name", workspaces)

    def test_worktree_remove_deletes_clean_exact_workspace(self) -> None:
        workspace = self.tmp / "clean-workspace"
        self.run_jj("-R", str(self.repo), "workspace", "add", str(workspace))

        result = self.run_shadow("worktree", "remove", str(workspace))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(workspace.exists())
        workspaces = self.run_jj("-R", str(self.repo), "workspace", "list").stdout
        self.assertNotIn("clean-workspace", workspaces)

    def test_worktree_remove_force_deletes_ignored_files(self) -> None:
        workspace = self.tmp / "forced-workspace"
        self.run_jj("-R", str(self.repo), "workspace", "add", str(workspace))
        (workspace / "credentials.secret").write_text("disposable\n")

        result = self.run_shadow("worktree", "remove", "-f", str(workspace))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(workspace.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
