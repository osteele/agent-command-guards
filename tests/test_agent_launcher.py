"""Tests for the generic agent launcher and its per-agent behaviour."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SHADOWS = REPO / "shadows"
LAUNCHER = REPO / "agent-launcher"
LAUNCHER_DIR = REPO / "launchers"
SHELL_INIT = LAUNCHER_DIR / "shell-init"

REPORT_ENVIRONMENT = (
    "#!/bin/sh\n"
    "printf 'args=%s\\n' \"$*\"\n"
    "printf 'guards=%s\\n' \"${AGENT_COMMAND_GUARDS_ACTIVE:-}\"\n"
    "printf 'guards_dir=%s\\n' \"${AGENT_COMMAND_GUARDS_DIR:-}\"\n"
    "printf 'zdotdir=%s\\n' \"${ZDOTDIR:-}\"\n"
    "printf 'first_on_path=%s\\n' \"$(command -v uv)\"\n"
    "printf 'agent_session=%s\\n' \"${AGENT_SESSION_ID:-}\"\n"
    "printf 'agent_session_pid=%s\\n' \"${AGENT_SESSION_PID:-}\"\n"
    "printf 'process_pid=%s\\n' \"$$\"\n"
    "printf 'claude_session=%s\\n' \"${CLAUDE_CODE_SESSION_ID:-}\"\n"
    "printf 'codex_thread=%s\\n' \"${CODEX_THREAD_ID:-}\"\n"
    "printf 'anthropic_key=%s\\n' \"${ANTHROPIC_API_KEY:-}\"\n"
    "printf 'openai_key=%s\\n' \"${OPENAI_API_KEY:-}\"\n"
    "printf 'resolved_omp=%s\\n' \"$(command -v omp 2>/dev/null || true)\"\n"
    "printf 'niceness=%s\\n' \"$(ps -o nice= -p $$ | tr -d ' ')\"\n"
    "printf 'soft_nofile=%s\\n' \"$(ulimit -Sn)\"\n"
)

OMP_RESUME_ID = "01a08a08-8116-7034-aab5-dab90db156e0"
CODEX_RESUME_ID = "01a08a58-6de3-7551-a929-ec2a73ea814e"
OPENCODE_RESUME_ID = "ses_f75a7947dffef4QMK32qW6RX8O"
AGY_RESUME_ID = "9006a41f-0f88-4c78-a766-ad1c6d226c60"

requires_posix = unittest.skipIf(
    os.name == "nt", "the launcher and its Zsh bridge are POSIX shell scripts"
)


@requires_posix
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
        self.environment.pop("OPENAI_API_KEY", None)
        self.environment.pop("ANTHROPIC_API_KEY", None)
        self.environment.pop("AGENT_LAUNCHER_KEEP_API_KEYS", None)

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
        self.assertIn(f"guards_dir={SHADOWS}", result.stdout)
        self.assertIn(f"first_on_path={SHADOWS / 'uv'}", result.stdout)

    def test_launches_agent_at_reduced_priority(self) -> None:
        self.install_real("kimi")
        result = self.launch("kimi")
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = min(19, os.getpriority(os.PRIO_PROCESS, 0) + 5)
        self.assertIn(f"niceness={expected}", result.stdout)

    def test_niceness_is_overridable_and_zero_disables(self) -> None:
        self.install_real("kimi")
        self.environment["AGENT_LAUNCHER_NICE"] = "0"
        result = self.launch("kimi")
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = os.getpriority(os.PRIO_PROCESS, 0)
        self.assertIn(f"niceness={expected}", result.stdout)

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

    def test_codex_gets_the_zsh_bridge(self) -> None:
        # codex re-sources shell configuration for its shell tool the same way
        # opencode does, so it needs the bridge as well.
        self.install_real("codex")
        result = self.launch("codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"zdotdir={SHELL_INIT}", result.stdout)

    def test_codex_raises_a_low_soft_open_file_limit(self) -> None:
        self.install_real("codex")
        launcher = str(LAUNCHER_DIR / "codex")
        result = subprocess.run(
            [
                "/bin/bash",
                "-c",
                'ulimit -Sn 256; exec "$@"',
                "bash",
                launcher,
            ],
            capture_output=True,
            check=False,
            env=self.environment,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("soft_nofile=65536", result.stdout)

    def session_id(self, result: subprocess.CompletedProcess[str]) -> str:
        for line in result.stdout.splitlines():
            if line.startswith("agent_session="):
                return line.removeprefix("agent_session=")
        self.fail(f"no agent_session line in output: {result.stdout!r}")

    def test_mints_a_session_id_for_agents_that_export_none(self) -> None:
        # kimi and opencode expose no per-session id of their own, so agent-mail
        # cannot address one of several sessions in a directory unless the id
        # comes from here.
        self.install_real("kimi")
        result = self.launch("kimi")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(self.session_id(result), "")

    def test_omp_shares_one_launcher_identity_with_its_subprocesses(self) -> None:
        self.install_real("omp")
        result = self.launch("omp")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(self.session_id(result), "")
        lines = dict(line.split("=", 1) for line in result.stdout.splitlines())
        self.assertEqual(lines["agent_session_pid"], lines["process_pid"])

    def test_omp_resume_strips_the_picker_prefix_from_its_argument(self) -> None:
        # OMP's picker shows ids as `omp:<uuid>` but resolves only the bare
        # uuid, so the launcher rewrites the pasted form before exec. The id
        # also becomes AGENT_SESSION_ID, so a resumed session keeps the
        # address it launched with.
        self.install_real("omp")
        result = self.launch("omp", "--resume", f"omp:{OMP_RESUME_ID}", "-p", "hi")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"args=--resume {OMP_RESUME_ID} -p hi", result.stdout)
        self.assertEqual(self.session_id(result), OMP_RESUME_ID)

    def test_omp_resume_strips_the_prefix_from_the_attached_form(self) -> None:
        self.install_real("omp")
        result = self.launch("omp", f"--resume=omp:{OMP_RESUME_ID}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"args=--resume={OMP_RESUME_ID}", result.stdout)
        self.assertEqual(self.session_id(result), OMP_RESUME_ID)

    def test_omp_resume_strips_the_prefix_from_the_short_flag(self) -> None:
        self.install_real("omp")
        result = self.launch("omp", "-r", f"omp:{OMP_RESUME_ID}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"args=-r {OMP_RESUME_ID}", result.stdout)
        self.assertEqual(self.session_id(result), OMP_RESUME_ID)

    def test_omp_resume_keeps_a_bare_uuid_unchanged(self) -> None:
        self.install_real("omp")
        result = self.launch("omp", "--resume", OMP_RESUME_ID)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"args=--resume {OMP_RESUME_ID}", result.stdout)
        self.assertEqual(self.session_id(result), OMP_RESUME_ID)

    def test_omp_passes_a_non_uuid_prefixed_value_through(self) -> None:
        # Shape-matched like the resume-id matcher: only `omp:<uuid>` counts,
        # so an `omp:`-prefixed path or name reaches OMP as typed.
        self.install_real("omp")
        result = self.launch("omp", "--resume", "omp:not-a-uuid")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("args=--resume omp:not-a-uuid", result.stdout)
        self.assertNotEqual(self.session_id(result), "omp:not-a-uuid")

    def test_other_agents_get_no_prefix_rewrite(self) -> None:
        # The rewrite is OMP-specific; kimi's own `session_` prefix must reach
        # it verbatim, resume identity included.
        self.install_real("kimi")
        kimi_id = f"session_{OMP_RESUME_ID}"
        result = self.launch("kimi", "--resume", kimi_id)
        self.assertIn(f"args=--resume {kimi_id}", result.stdout)
        self.assertEqual(self.session_id(result), kimi_id)

    def test_codex_resume_strips_the_prefix_from_its_subcommand_value(self) -> None:
        self.install_real("codex")
        result = self.launch("codex", "resume", f"codex:{CODEX_RESUME_ID}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"args=resume {CODEX_RESUME_ID}", result.stdout)
        self.assertEqual(self.session_id(result), CODEX_RESUME_ID)

    def test_kimi_resume_strips_the_qualified_canonical_id(self) -> None:
        # AgentsView qualifies kimi's native session_<uuid> with machine and
        # channel segments; the native id is everything through the last
        # colon, matching launchers/agent-model's kimi handling.
        self.install_real("kimi")
        kimi_id = f"session_{OMP_RESUME_ID}"
        canonical = f"kimi:wd_weft_b639d530ae70:main:{kimi_id}"
        result = self.launch("kimi", "--session", canonical)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"args=--session {kimi_id}", result.stdout)
        self.assertEqual(self.session_id(result), kimi_id)

    def test_kimi_resume_strips_the_simple_prefixed_form(self) -> None:
        self.install_real("kimi")
        kimi_id = f"session_{OMP_RESUME_ID}"
        result = self.launch("kimi", "-r", f"kimi:{kimi_id}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"args=-r {kimi_id}", result.stdout)
        self.assertEqual(self.session_id(result), kimi_id)

    def test_opencode_resume_strips_the_prefix(self) -> None:
        self.install_real("opencode")
        result = self.launch("opencode", "-s", f"opencode:{OPENCODE_RESUME_ID}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"args=-s {OPENCODE_RESUME_ID}", result.stdout)
        self.assertEqual(self.session_id(result), OPENCODE_RESUME_ID)

    def test_agy_resume_strips_the_antigravity_cli_prefix(self) -> None:
        # AgentsView indexes Antigravity under its product CLI name while the
        # launcher symlink is `agy`.
        self.install_real("agy")
        result = self.launch(
            "agy", "--conversation", f"antigravity-cli:{AGY_RESUME_ID}"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"args=--conversation {AGY_RESUME_ID}", result.stdout)
        self.assertEqual(self.session_id(result), AGY_RESUME_ID)

    def test_a_foreign_agentsview_prefix_passes_through(self) -> None:
        # Each launcher strips only its own agent's prefix: a codex id pasted
        # into omp is not an omp session, and omp's own not-found error is the
        # truthful answer.
        self.install_real("omp")
        result = self.launch("omp", "--resume", f"codex:{CODEX_RESUME_ID}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"args=--resume codex:{CODEX_RESUME_ID}", result.stdout)
        self.assertNotEqual(self.session_id(result), f"codex:{CODEX_RESUME_ID}")

    def test_a_prefix_agentsview_does_not_use_passes_through(self) -> None:
        # `agy:` is not an AgentsView form (`antigravity-cli:` is), so a
        # conversation value carrying it reaches the agent as typed.
        self.install_real("agy")
        result = self.launch("agy", "--conversation", f"agy:{AGY_RESUME_ID}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"args=--conversation agy:{AGY_RESUME_ID}", result.stdout)

    def test_omp_update_exposes_the_real_binary_to_its_updater(self) -> None:
        real = self.install_real("omp")
        result = self.launch("omp", "update")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"resolved_omp={real}", result.stdout)
        self.assertIn(f"first_on_path={SHADOWS / 'uv'}", result.stdout)

    def test_omp_does_not_inherit_provider_api_keys(self) -> None:
        # OMP reads an env key as being logged in, bills it at API rates, and
        # lets it suppress the stored subscription credential. The keys reach
        # every interactive shell from the keychain, so they arrive inherited.
        self.install_real("omp")
        self.environment["ANTHROPIC_API_KEY"] = "sk-ant-inherited"
        self.environment["OPENAI_API_KEY"] = "sk-openai-inherited"
        result = self.launch("omp")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("anthropic_key=\n", result.stdout)
        self.assertIn("openai_key=\n", result.stdout)

    def test_omp_keeps_provider_api_keys_when_asked(self) -> None:
        self.install_real("omp")
        self.environment["ANTHROPIC_API_KEY"] = "sk-ant-inherited"
        self.environment["OPENAI_API_KEY"] = "sk-openai-inherited"
        self.environment["AGENT_LAUNCHER_KEEP_API_KEYS"] = "1"
        result = self.launch("omp")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("anthropic_key=sk-ant-inherited\n", result.stdout)
        self.assertIn("openai_key=sk-openai-inherited\n", result.stdout)

    def test_other_agents_keep_provider_api_keys(self) -> None:
        # Only OMP resolves provider credentials from the environment without
        # an approval gate; the other agents have no reason to lose the keys.
        self.install_real("kimi")
        self.environment["ANTHROPIC_API_KEY"] = "sk-ant-inherited"
        self.environment["OPENAI_API_KEY"] = "sk-openai-inherited"
        result = self.launch("kimi")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("anthropic_key=sk-ant-inherited\n", result.stdout)
        self.assertIn("openai_key=sk-openai-inherited\n", result.stdout)

    def test_each_launch_gets_a_distinct_session_id(self) -> None:
        self.install_real("kimi")
        first = self.launch("kimi")
        second = self.launch("kimi")
        self.assertNotEqual(self.session_id(first), self.session_id(second))

    def test_an_inherited_session_id_is_replaced_not_reused(self) -> None:
        # An agent started from inside another agent's shell inherits that
        # parent's ids. Answering to them would attribute this session's work to
        # one specific wrong session, which is worse than no id at all.
        self.install_real("kimi")
        self.environment["AGENT_SESSION_ID"] = "parent-agent"
        self.environment["CLAUDE_CODE_SESSION_ID"] = "parent-claude"
        self.environment["CODEX_THREAD_ID"] = "parent-codex"
        result = self.launch("kimi")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(self.session_id(result), "parent-agent")
        self.assertNotEqual(self.session_id(result), "")
        # The native ids resolve ahead of AGENT_SESSION_ID, so leaving either in
        # place would let the inherited identity win anyway.
        self.assertIn("claude_session=\n", result.stdout)
        self.assertIn("codex_thread=\n", result.stdout)

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


@requires_posix
class ShellBridgeTest(unittest.TestCase):
    def run_bridge_zshenv(self, home: Path) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["AGENT_COMMAND_GUARDS_DIR"] = str(SHADOWS)
        environment["AGENT_LAUNCHER_ORIGINAL_ZDOTDIR"] = str(home)
        environment["ZDOTDIR"] = str(SHELL_INIT)
        environment["HOME"] = str(home)
        environment["TERM"] = "dumb"
        return subprocess.run(
            # -f skips this machine's startup files; the bridge file under
            # test is then sourced explicitly, exactly as zsh would.
            [
                "/bin/zsh",
                "-f",
                "-c",
                'source "$ZDOTDIR/.zshenv"; echo $PATH; echo zdotdir=$ZDOTDIR',
            ],
            capture_output=True,
            check=False,
            env=environment,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )

    def test_startup_files_restore_the_shadow_directory(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            home = Path(name)
            (home / ".zshrc").write_text('export PATH="/usr/local/bin:$PATH"\n')
            environment = dict(os.environ)
            environment["AGENT_COMMAND_GUARDS_DIR"] = str(SHADOWS)
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
            result.stdout.strip().startswith(f"{SHADOWS}:"),
            f"shadow directory is not first: {result.stdout.strip()[:200]}",
        )

    def test_user_zshenv_repointing_zdotdir_does_not_break_the_bridge(self) -> None:
        # Frameworks and version managers repoint ZDOTDIR from .zshenv; the
        # bridge must still find its own prepend script afterwards instead of
        # erroring against the replacement directory. Clobbering every helper
        # variable the bridge might use must not matter either, so the test
        # trashes them all.
        with tempfile.TemporaryDirectory() as name:
            home = Path(name)
            (home / ".zshenv").write_text(
                f'export ZDOTDIR="{home}"\n'
                "bridge_dir=garbage\n"
                "original_zdotdir=garbage\n"
                "unset bridge_dir original_zdotdir\n"
            )
            result = self.run_bridge_zshenv(home)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("no such file", result.stderr.lower())
        # The repoint actually happened, so the hazard was exercised...
        self.assertIn(f"zdotdir={home}", result.stdout)
        # ...and the shadows are still at the front of PATH.
        first_path_line = result.stdout.splitlines()[0]
        self.assertTrue(
            first_path_line.startswith(f"{SHADOWS}:"),
            f"shadow directory is not first: {first_path_line[:200]}",
        )


if __name__ == "__main__":
    unittest.main()
