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
        self.run_jj("git", "init", "--no-colocate", str(self.repo), cwd=self.tmp)
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
        self.assertIn("On branch local/jj-shadow-head", result.stdout)

    def test_dash_c_finds_jj_repository(self) -> None:
        result = self.run_shadow("-C", str(self.repo), "status", cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("On branch local/jj-shadow-head", result.stdout)

    def test_pure_jj_status_preserves_git_porcelain(self) -> None:
        (self.repo / "tracked.txt").write_text("modified\n")
        result = self.run_shadow("status", "--short")
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

    def test_pure_jj_diff_reads_working_tree(self) -> None:
        (self.repo / "tracked.txt").write_text("modified\n")
        result = self.run_shadow("diff", "HEAD")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("-initial", result.stdout)
        self.assertIn("+modified", result.stdout)

    def test_fresh_pure_repo_status_reports_empty_repository(self) -> None:
        # Before the zero-OID handling, a repo with no parent commit fell
        # through to plain git and died with "not a git repository".
        fresh = self.tmp / "fresh-pure"
        self.run_jj("git", "init", "--no-colocate", str(fresh), cwd=self.tmp)
        (fresh / "untracked.txt").write_text("new\n")

        result = self.run_shadow("status", cwd=fresh)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("On branch local/jj-shadow-head", result.stdout)
        self.assertIn("No commits yet", result.stdout)
        self.assertIn("untracked.txt", result.stdout)

    def test_fresh_pure_repo_log_exits_cleanly(self) -> None:
        fresh = self.tmp / "fresh-pure"
        self.run_jj("git", "init", "--no-colocate", str(fresh), cwd=self.tmp)

        result = self.run_shadow("log", "--oneline", cwd=fresh)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_fresh_colocated_repo_leaves_git_head_alone(self) -> None:
        # A fresh co-located checkout has no parent commit either, but its
        # .git HEAD already serves the user; repointing it at an unborn
        # machine ref would rename their checkout branch out from under them.
        fresh = self.tmp / "fresh-colocated"
        self.run_jj("git", "init", "--colocate", str(fresh), cwd=self.tmp)
        head_before = (fresh / ".git" / "HEAD").read_text()

        result = self.run_shadow("status", cwd=fresh)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No commits yet", result.stdout)
        self.assertNotIn("local/jj-shadow-head", result.stdout)
        self.assertEqual((fresh / ".git" / "HEAD").read_text(), head_before)

    def test_read_command_retires_the_pre_rename_jj_head_ref(self) -> None:
        # Repositories served by the pre-rename shadow carry refs/heads/jj-head,
        # which jj imports as a bookmark with no local/ marker. A read command
        # through the shadow must retire it once the new ref is in place.
        store = self.repo / ".jj" / "repo" / "store" / "git"
        parent = self.run_jj(
            "-R", str(self.repo), "log", "-r", "@-", "--no-graph", "-T", "commit_id"
        ).stdout.strip()
        system_git = "/usr/bin/git"
        subprocess.run(
            [
                system_git,
                "--git-dir",
                str(store),
                "update-ref",
                "refs/heads/jj-head",
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
        self.assertIn("refs/heads/local/jj-shadow-head", refs)

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
