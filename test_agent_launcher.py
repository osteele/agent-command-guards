"""Tests for the generic agent launcher and its per-agent behaviour."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAUNCHER = HERE / "agent-launcher"
LAUNCHER_DIR = HERE / "launchers"
SHELL_INIT = LAUNCHER_DIR / "shell-init"

REPORT_ENVIRONMENT = (
    "#!/bin/sh\n"
    'printf \'args=%s\\n\' "$*"\n'
    'printf \'guards=%s\\n\' "${AGENT_COMMAND_GUARDS_ACTIVE:-}"\n'
    'printf \'guards_dir=%s\\n\' "${AGENT_COMMAND_GUARDS_DIR:-}"\n'
    'printf \'zdotdir=%s\\n\' "${ZDOTDIR:-}"\n'
    'printf \'first_on_path=%s\\n\' "$(command -v uv)"\n'
)


class AgentLauncherTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.real_bin = self.tmp / "bin"
        self.real_bin.mkdir()
        self.fake_home = self.tmp / "home"
        self.fake_home.mkdir()
        self.environment = dict(os.environ)
        self.environment["PATH"] = f"{LAUNCHER_DIR}:{self.real_bin}:/usr/bin:/bin"
        # An empty HOME keeps the installer fallbacks (~/.kimi-code/bin/kimi)
        # from reaching the real agent binaries on this machine.
        self.environment["HOME"] = str(self.fake_home)
        self.environment.pop("ZDOTDIR", None)
        self.environment.pop("AGENT_COMMAND_GUARDS_ACTIVE", None)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def install_real(self, name: str) -> Path:
        real = self.real_bin / name
        real.write_text(REPORT_ENVIRONMENT)
        real.chmod(0o755)
        return real

    def launch(self, name: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(LAUNCHER_DIR / name), *args],
            capture_output=True,
            check=False,
            env=self.environment,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )

    def test_launcher_refuses_to_run_unnamed(self) -> None:
        result = subprocess.run(
            [str(LAUNCHER)],
            capture_output=True,
            check=False,
            env=self.environment,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("agent-named symlink", result.stderr)

    def test_skips_itself_when_finding_the_real_binary(self) -> None:
        # The launcher sits ahead of the real binary on PATH; resolving `kimi`
        # naively would re-exec the launcher forever.
        self.install_real("kimi")
        result = self.launch("kimi", "--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("args=--version", result.stdout)

    def test_activates_the_guards_for_the_agent(self) -> None:
        self.install_real("kimi")
        result = self.launch("kimi")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("guards=1", result.stdout)
        self.assertIn(f"guards_dir={HERE}", result.stdout)
        self.assertIn(f"first_on_path={HERE / 'uv'}", result.stdout)

    def test_kimi_does_not_get_the_zsh_bridge(self) -> None:
        # kimi runs tool commands through `sh -c`, which never reads zsh
        # startup files, so redirecting ZDOTDIR would be inert.
        self.install_real("kimi")
        result = self.launch("kimi")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("zdotdir=\n", result.stdout)

    def test_opencode_gets_the_zsh_bridge(self) -> None:
        # opencode's shell snapshot sources ${ZDOTDIR:-$HOME}/.zshrc, which
        # would restore mise's competing uv shim ahead of the shadows.
        self.install_real("opencode")
        result = self.launch("opencode")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"zdotdir={SHELL_INIT}", result.stdout)

    def test_missing_binary_is_reported(self) -> None:
        result = self.launch("kimi")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Could not find the real kimi binary", result.stderr)

    def test_doctor_reports_the_resolved_binary(self) -> None:
        self.install_real("opencode")
        result = self.launch("opencode", "wrapper", "doctor")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Real opencode: {self.real_bin / 'opencode'}", result.stdout)
        self.assertIn("Launcher is first on PATH", result.stdout)

    def test_unknown_wrapper_subcommand_reaches_the_agent(self) -> None:
        # `wrapper` is only intercepted for doctor and path; anything else is
        # the agent's own CLI surface.
        self.install_real("kimi")
        result = self.launch("kimi", "wrapper", "install")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("args=wrapper install", result.stdout)


class ShellBridgeTest(unittest.TestCase):
    def test_startup_files_restore_the_shadow_directory(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            home = Path(name)
            (home / ".zshrc").write_text('export PATH="/usr/local/bin:$PATH"\n')
            environment = dict(os.environ)
            environment["AGENT_COMMAND_GUARDS_DIR"] = str(HERE)
            environment["AGENT_LAUNCHER_ORIGINAL_ZDOTDIR"] = str(home)
            environment["ZDOTDIR"] = str(SHELL_INIT)
            environment["HOME"] = str(home)
            environment["TERM"] = "dumb"
            result = subprocess.run(
                # -f skips this machine's startup files; the bridge file under
                # test is then sourced explicitly, exactly as zsh would.
                ["/bin/zsh", "-f", "-c", 'source "$ZDOTDIR/.zshrc"; echo $PATH'],
                capture_output=True,
                check=False,
                env=environment,
                stdin=subprocess.DEVNULL,
                text=True,
                timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        # The user's own .zshrc ran, and the shadows are still in front of it.
        self.assertIn("/usr/local/bin", result.stdout)
        self.assertTrue(
            result.stdout.strip().startswith(f"{HERE}:"),
            f"shadow directory is not first: {result.stdout.strip()[:200]}",
        )


if __name__ == "__main__":
    unittest.main()
