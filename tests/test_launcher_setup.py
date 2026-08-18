"""Tests for launchers/setup: install, uninstall, and the generated env file."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LAUNCHER_DIR = REPO / "launchers"
SETUP = LAUNCHER_DIR / "setup"
SHADOWS = REPO / "shadows"
AGENTS = ("kimi", "opencode", "codex")
RC_FILES = (".zshenv", ".zshrc", ".bashrc")
BLOCK_START = "# >>> agent-launchers initialize >>>"


class LauncherSetupTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.bin_dir = self.tmp / "bin"
        self.environment = dict(os.environ)
        self.environment["HOME"] = str(self.home)
        self.environment["AGENT_LAUNCHER_BIN_DIR"] = str(self.bin_dir)
        self.environment["PATH"] = "/usr/bin:/bin"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_setup(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SETUP), *args],
            capture_output=True,
            check=False,
            env=self.environment,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )

    def seed_rc_files(self) -> None:
        # Sentinels prove uninstall and dry-run preserve unrelated content.
        for name in RC_FILES:
            (self.home / name).write_text(f"export SENTINEL_{name[1:-1].upper()}=1\n")

    def sentinel(self, name: str) -> str:
        return f"export SENTINEL_{name[1:-1].upper()}=1"

    def test_dry_run_touches_nothing(self) -> None:
        self.seed_rc_files()
        result = self.run_setup("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in RC_FILES:
            self.assertEqual((self.home / name).read_text(), f"{self.sentinel(name)}\n")
        self.assertFalse(self.bin_dir.exists())
        self.assertFalse((self.home / ".config").exists())

    def test_install_links_binaries_and_configures_rc_files(self) -> None:
        self.seed_rc_files()
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        for agent in AGENTS:
            link = self.bin_dir / agent
            self.assertTrue(link.is_symlink(), link)
            self.assertEqual(os.path.realpath(link), str(REPO / "agent-launcher"))
        env_file = self.home / ".config" / "agent-launchers" / "env"
        self.assertTrue(env_file.is_file())
        self.assertIn(f'export PATH="{LAUNCHER_DIR}:$PATH"', env_file.read_text())
        for name in RC_FILES:
            content = (self.home / name).read_text()
            self.assertEqual(content.count(BLOCK_START), 1, name)
            self.assertIn(self.sentinel(name), content)
            self.assertIn('. "$HOME/.config/agent-launchers/env"', content)

    def test_install_leaves_missing_rc_files_uncreated(self) -> None:
        (self.home / ".zshenv").write_text(f"{self.sentinel('.zshenv')}\n")

        result = self.run_setup()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.home / ".zshenv").read_text().count(BLOCK_START), 1)
        self.assertFalse((self.home / ".zshrc").exists())
        self.assertFalse((self.home / ".bashrc").exists())

    def test_install_is_idempotent(self) -> None:
        self.seed_rc_files()
        self.assertEqual(self.run_setup().returncode, 0)
        second = self.run_setup()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(second.stdout.count("Already configured"), 3)
        self.assertEqual(second.stdout.count("Already linked"), 3)
        for name in RC_FILES:
            self.assertEqual((self.home / name).read_text().count(BLOCK_START), 1)

    def test_install_refuses_to_replace_a_foreign_binary(self) -> None:
        self.bin_dir.mkdir(parents=True)
        foreign = self.bin_dir / "opencode"
        foreign.write_text("#!/bin/sh\nexit 42\n")

        result = self.run_setup()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Refusing to replace", result.stderr)
        self.assertEqual(foreign.read_text(), "#!/bin/sh\nexit 42\n")

    def test_uninstall_removes_links_blocks_and_env(self) -> None:
        self.seed_rc_files()
        self.assertEqual(self.run_setup().returncode, 0)

        result = self.run_setup("--uninstall")

        self.assertEqual(result.returncode, 0, result.stderr)
        for agent in AGENTS:
            self.assertFalse((self.bin_dir / agent).exists())
        self.assertFalse((self.home / ".config" / "agent-launchers" / "env").exists())
        for name in RC_FILES:
            content = (self.home / name).read_text()
            self.assertNotIn(BLOCK_START, content)
            self.assertIn(self.sentinel(name), content)

    def test_uninstall_preserves_foreign_binaries(self) -> None:
        self.bin_dir.mkdir(parents=True)
        foreign = self.bin_dir / "kimi"
        foreign.write_text("mine\n")

        result = self.run_setup("--uninstall")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(foreign.read_text(), "mine\n")

    def test_uninstall_without_install_is_a_noop(self) -> None:
        result = self.run_setup("--uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.bin_dir.exists())
        self.assertFalse((self.home / ".config").exists())


class GeneratedEnvTest(unittest.TestCase):
    """The generated env file must restore the guards in sh, Bash, and Zsh."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        home = Path(cls._tmp.name) / "home"
        home.mkdir()
        bin_dir = Path(cls._tmp.name) / "bin"
        environment = dict(os.environ)
        environment["HOME"] = str(home)
        environment["AGENT_LAUNCHER_BIN_DIR"] = str(bin_dir)
        environment["PATH"] = "/usr/bin:/bin"
        subprocess.run(
            [str(SETUP)],
            capture_output=True,
            check=True,
            env=environment,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )
        cls.home = home
        cls.env_file = home / ".config" / "agent-launchers" / "env"

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def source_env(
        self, shell: str, path_value: str, guards: str | None
    ) -> subprocess.CompletedProcess[str]:
        environment = {"HOME": str(self.home), "PATH": path_value}
        if guards is not None:
            environment["AGENT_COMMAND_GUARDS_DIR"] = guards
        return subprocess.run(
            [
                shell,
                *(["-f"] if shell.endswith("zsh") else []),
                "-c",
                f'. "{self.env_file}"; printf "%s\\n" "$PATH" '
                "; command -v agent_guards_restore || echo function-gone",
            ],
            capture_output=True,
            check=False,
            env=environment,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )

    def test_moves_the_guards_to_the_front_in_every_shell(self) -> None:
        # An agent re-sourced its shell configuration: version managers sit in
        # front of the guards, which must return to the front, not multiply.
        for shell in ("/bin/sh", "/bin/bash", "/bin/zsh"):
            with self.subTest(shell=shell):
                result = self.source_env(
                    shell, f"/usr/bin:/bin:{SHADOWS}", str(SHADOWS)
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                first_line = result.stdout.splitlines()[0]
                self.assertTrue(
                    first_line.startswith(f"{SHADOWS}:{LAUNCHER_DIR}"),
                    f"{shell}: guards not restored to front: {first_line}",
                )
                self.assertEqual(first_line.count(str(SHADOWS)), 1)
                self.assertIn("function-gone", result.stdout)
                self.assertNotIn("agent_guards_restore\n", result.stdout)

    def test_is_a_noop_without_the_guards_on_path(self) -> None:
        # An ordinary shell has no guards directory on PATH; sourcing the env
        # must not put one there.
        for shell in ("/bin/sh", "/bin/bash", "/bin/zsh"):
            with self.subTest(shell=shell):
                result = self.source_env(shell, "/usr/bin:/bin", None)
                self.assertEqual(result.returncode, 0, result.stderr)
                first_line = result.stdout.splitlines()[0]
                self.assertTrue(first_line.startswith(f"{LAUNCHER_DIR}:"))
                self.assertNotIn(str(SHADOWS), first_line)


if __name__ == "__main__":
    unittest.main()
