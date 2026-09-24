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
        # Skip the shadows directory: its jj wrapper would otherwise stand in for
        # the real binary whose directory this test puts on PATH.
        search_path = os.pathsep.join(
            entry
            for entry in os.environ.get("PATH", "").split(os.pathsep)
            if entry and Path(entry).resolve() != SHADOWS.resolve()
        )
        jj = shutil.which("jj", path=search_path)
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
            cwd=cwd or self.repo,
            env=self.environment,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def run_shadow(
        self,
        *args: str,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(GIT_SHADOW), *args],
            capture_output=True,
            check=False,
            cwd=cwd or self.repo,
            env=env if env is not None else self.environment,
            text=True,
        )

    def test_read_command_after_global_option_uses_jj_repository(self) -> None:
        """ExecuteAllowedGitRead preserves Git globals and reads the current tree."""
        (self.repo / "tracked.txt").write_text("modified\n")
        result = self.run_shadow("--no-pager", "status", "--short")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, " M tracked.txt\n")

    def test_read_command_refuses_when_jj_synchronization_fails(self) -> None:
        """GitReadSynchronization must not expose a stale successful Git read."""
        binaries = self.tmp / "failing-jj"
        binaries.mkdir()
        failing_jj = binaries / "jj"
        failing_jj.write_text("#!/bin/sh\nexit 37\n")
        failing_jj.chmod(0o755)
        environment = {
            **self.environment,
            "PATH": f"{binaries}:{self.environment['PATH']}",
        }

        result = self.run_shadow("log", "--oneline", env=environment)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_checkout_is_denied_without_discarding_working_copy_change(self) -> None:
        tracked = self.repo / "tracked.txt"
        tracked.write_text("uncommitted refactor\n")

        result = self.run_shadow("checkout", "--", "tracked.txt")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(tracked.read_text(), "uncommitted refactor\n")
        self.assertIn("refusing unsupported `git checkout`", result.stderr)

    def test_explicit_git_dir_does_not_bypass_checkout_denial(self) -> None:
        tracked = self.repo / "tracked.txt"
        tracked.write_text("uncommitted refactor\n")

        result = self.run_shadow("--git-dir=.git", "checkout", "--", "tracked.txt")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(tracked.read_text(), "uncommitted refactor\n")
        self.assertIn("refusing unsupported `git checkout`", result.stderr)

    def test_git_directory_environment_does_not_bypass_checkout_denial(self) -> None:
        tracked = self.repo / "tracked.txt"
        tracked.write_text("uncommitted refactor\n")
        environment = {
            **self.environment,
            "GIT_DIR": str(self.repo / ".git"),
            "GIT_WORK_TREE": str(self.repo),
        }

        result = self.run_shadow(
            "checkout", "--", "tracked.txt", cwd=self.tmp, env=environment
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(tracked.read_text(), "uncommitted refactor\n")
        self.assertIn("refusing unsupported `git checkout`", result.stderr)

    def test_dash_c_rebases_explicit_paths_back_into_jj_repository(self) -> None:
        tracked = self.repo / "tracked.txt"
        tracked.write_text("uncommitted refactor\n")
        subdir = self.repo / "subdir"
        subdir.mkdir()

        result = self.run_shadow(
            "--git-dir=../.git",
            "--work-tree=..",
            "-C",
            str(subdir),
            "checkout",
            "--",
            "tracked.txt",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(tracked.read_text(), "uncommitted refactor\n")
        self.assertIn("refusing unsupported `git checkout`", result.stderr)

    def test_explicit_external_repository_bypasses_jj_denial(self) -> None:
        plain = self.tmp / "plain"
        plain.mkdir()

        init_result = self.run_shadow(
            f"--git-dir={plain / '.git'}",
            f"--work-tree={plain}",
            "init",
        )
        self.assertEqual(init_result.returncode, 0, init_result.stderr)
        self.assertTrue((plain / ".git").is_dir())

        result = self.run_shadow(
            f"--git-dir={plain / '.git'}",
            f"--work-tree={plain}",
            "checkout",
            "--orphan",
            "topic",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("refusing unsupported", result.stderr)

    def test_mutating_branch_command_is_denied(self) -> None:
        result = self.run_shadow("branch", "topic")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing unsupported `git branch`", result.stderr)

    def test_read_only_branch_command_is_allowlisted(self) -> None:
        result = self.run_shadow("branch", "--show-current")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("supported through a compatibility layer", result.stderr)

    def test_branch_list_pattern_named_like_mutating_option_is_allowlisted(
        self,
    ) -> None:
        result = self.run_shadow("branch", "--list", "--", "--delete")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("supported through a compatibility layer", result.stderr)

    # --- bash 3.2 empty-array expansion -----------------------------------
    #
    # macOS ships bash 3.2, where "${arr[@]}" on an *empty* array is an
    # unbound-variable error under `set -u`. Every branch of the wrapper that
    # expands a subcommand's argument array must therefore survive being given
    # no arguments at all. The three tests above all pass arguments, which is
    # why a regression that broke bare `git branch` outright shipped and stayed
    # green on the macOS CI job.

    def test_bare_branch_survives_empty_argument_array(self) -> None:
        result = self.run_shadow("branch")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("supported through a compatibility layer", result.stderr)
        self.assertNotIn("unbound variable", result.stderr)

    def test_bare_worktree_refuses_rather_than_crashing(self) -> None:
        result = self.run_shadow("worktree")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing unsupported `git worktree`", result.stderr)
        self.assertNotIn("unbound variable", result.stderr)

    def test_bare_commit_survives_empty_argument_array(self) -> None:
        """TranslateGitMutation commits content even with an empty argument list."""
        (self.repo / "tracked.txt").write_text("committed\n")
        environment = dict(self.environment)
        environment["JJ_EDITOR"] = "true"
        result = self.run_shadow("commit", env=environment)

        self.assertEqual(result.returncode, 0, result.stderr)
        committed = self.run_jj("-R", str(self.repo), "file", "show", "-r", "@-", "tracked.txt")
        self.assertEqual(committed.stdout, "committed\n")

    def test_add_and_commit_preserve_message_and_unstaged_changes(self) -> None:
        """TreatGitAddAsTracked and TranslateGitMutation preserve values, not Git-only flags."""
        (self.repo / "added.txt").write_text("new content\n")
        added = self.run_shadow("add", "added.txt")
        self.assertEqual(added.returncode, 0, added.stderr)
        native_git = shutil.which("git", path=os.defpath)
        staged = subprocess.run(
            [native_git, "-C", str(self.repo), "diff", "--cached", "--name-only"],
            capture_output=True, text=True, timeout=30, check=True,
        )
        self.assertEqual(staged.stdout, "")

        committed = self.run_shadow(
            "commit", "-a", "-p", "--allow-empty", "--no-edit", "-m", "--amend",
        )
        self.assertEqual(committed.returncode, 0, committed.stderr)
        message = self.run_jj("-R", str(self.repo), "log", "-r", "@-", "--no-graph", "-T", "description")
        self.assertEqual(message.stdout, "--amend\n")
        content = self.run_jj("-R", str(self.repo), "file", "show", "-r", "@-", "added.txt")
        self.assertEqual(content.stdout, "new content\n")

    def test_amend_describes_current_change_without_creating_another(self) -> None:
        """TranslateGitMutation maps amend to describing the current jj change."""
        before = self.run_jj("-R", str(self.repo), "log", "-r", "@", "--no-graph", "-T", "change_id").stdout
        (self.repo / "tracked.txt").write_text("amended content\n")

        result = self.run_shadow("commit", "--amend", "--no-edit", "-m", "amended description")

        self.assertEqual(result.returncode, 0, result.stderr)
        after = self.run_jj("-R", str(self.repo), "log", "-r", "@", "--no-graph", "-T", "change_id").stdout
        self.assertEqual(after, before)
        message = self.run_jj("-R", str(self.repo), "log", "-r", "@", "--no-graph", "-T", "description")
        self.assertEqual(message.stdout, "amended description\n")
        content = self.run_jj("-R", str(self.repo), "file", "show", "-r", "@", "tracked.txt")
        self.assertEqual(content.stdout, "amended content\n")

    def test_worktree_add_registers_a_jj_workspace(self) -> None:
        """TranslateGitMutation creates a usable workspace visible to both listing surfaces."""
        workspace = self.tmp / "created-workspace"

        result = self.run_shadow("worktree", "add", str(workspace))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((workspace / "tracked.txt").read_text(), "initial\n")
        roots = self.run_jj("-R", str(self.repo), "workspace", "list", "-T", 'root ++ "\\n"')
        self.assertIn(str(workspace.resolve()), roots.stdout.splitlines())
        listed = self.run_shadow("worktree", "list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("created-workspace:", listed.stdout)

    def test_worktree_prune_updates_a_stale_working_copy(self) -> None:
        """TranslateGitMutation reconciles the working copy rather than invoking Git prune."""
        workspace = self.tmp / "editing-workspace"
        self.run_jj("-R", str(self.repo), "workspace", "add", str(workspace))
        (workspace / "tracked.txt").write_text("updated elsewhere\n")
        self.run_jj("-R", str(workspace), "squash", "--into", "default@", "-m", "updated")
        self.assertEqual((self.repo / "tracked.txt").read_text(), "initial\n")

        result = self.run_shadow("worktree", "prune")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.repo / "tracked.txt").read_text(), "updated elsewhere\n")

    def test_unknown_worktree_command_is_denied(self) -> None:
        result = self.run_shadow("worktree", "lock", str(self.repo))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing unsupported `git worktree`", result.stderr)

    def test_explicit_git_dir_preserves_worktree_list_translation(self) -> None:
        result = self.run_shadow("--git-dir=.git", "worktree", "list")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("default:", result.stdout)
        self.assertNotIn("refusing unsupported", result.stderr)

    def test_commands_outside_jj_repositories_still_delegate(self) -> None:
        plain_git_repo = self.tmp / "plain-git"
        plain_git_repo.mkdir()

        result = self.run_shadow("init", cwd=plain_git_repo)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((plain_git_repo / ".git").is_dir())

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

    def test_force_cannot_remove_primary_workspace(self) -> None:
        """RejectProtectedWorkspaceRemoval takes precedence over force."""
        result = self.run_shadow("worktree", "remove", "--force", str(self.repo))

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.repo / "tracked.txt").read_text(), "initial\n")
        self.run_jj("-R", str(self.repo), "status")

    def test_force_cannot_remove_current_workspace_from_another_repository_context(self) -> None:
        """The actual current workspace remains protected even with Git -C."""
        workspace = self.tmp / "current-workspace"
        self.run_jj("-R", str(self.repo), "workspace", "add", str(workspace))

        result = self.run_shadow(
            "-C", str(self.repo), "worktree", "remove", "--force", str(workspace),
            cwd=workspace,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((workspace / "tracked.txt").read_text(), "initial\n")
        self.assertIn("current-workspace", self.run_jj("-R", str(self.repo), "workspace", "list").stdout)

    def test_force_cannot_remove_workspace_through_symlink(self) -> None:
        """A symlink cannot turn forced removal into permission for its target."""
        workspace = self.tmp / "linked-workspace"
        self.run_jj("-R", str(self.repo), "workspace", "add", str(workspace))
        alias = self.tmp / "workspace-alias"
        alias.symlink_to(workspace, target_is_directory=True)

        result = self.run_shadow("worktree", "remove", "--force", f"{alias}/")

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(alias.is_symlink())
        self.assertEqual((workspace / "tracked.txt").read_text(), "initial\n")
        self.assertIn("linked-workspace", self.run_jj("-R", str(self.repo), "workspace", "list").stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
