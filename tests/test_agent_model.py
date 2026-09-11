"""Tests for model-first interactive command dispatch."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
AGENT_MODEL = REPO / "launchers" / "agent-model"
SHELL_OVERLAY = REPO / "shell" / "agent-models.sh"


class AgentModelTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.bin_dir = self.tmp / "bin"
        self.home.mkdir()
        self.bin_dir.mkdir()
        self.environment = dict(os.environ)
        self.environment["HOME"] = str(self.home)
        self.environment.pop("XDG_CONFIG_HOME", None)
        self.environment["PATH"] = f"{self.bin_dir}:{os.defpath}"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_model(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(AGENT_MODEL), *arguments],
            capture_output=True,
            check=False,
            env=self.environment,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )

    def install_recorder(self, command: str) -> None:
        path = self.bin_dir / command
        path.write_text('#!/bin/sh\nprintf \'%s\\n\' "$0" "$@"\n')
        path.chmod(0o755)

    def install_agentsview(self, agent: str) -> None:
        path = self.bin_dir / "agentsview"
        path.write_text(
            f"#!/bin/sh\nprintf '%s\\n' '{{\"agent\":\"{agent}\"}}'\n"
        )
        path.chmod(0o755)

    def config(self) -> Path:
        return self.home / ".config" / "agent-models" / "config.toml"

    def run_with_defaults(
        self, defaults: str, *arguments: str
    ) -> subprocess.CompletedProcess[str]:
        """Run a copy of the launcher against a substitute defaults file.

        The launcher finds the file beside its own directory, so the copy needs
        the same two-level layout the repository has.
        """
        root = self.tmp / "repo"
        (root / "launchers").mkdir(parents=True, exist_ok=True)
        launcher = root / "launchers" / "agent-model"
        launcher.write_text(AGENT_MODEL.read_text())
        launcher.chmod(0o755)
        (root / "agent-models.toml").write_text(defaults)
        return subprocess.run(
            [sys.executable, str(launcher), *arguments],
            capture_output=True,
            check=False,
            env=self.environment,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )

    def test_missing_config_uses_the_shipped_defaults(self) -> None:
        expected = {
            "claude": "anthropic/claude-opus-5",
            "fable": "anthropic/claude-fable-5-1",
            "codex": "openai-codex/gpt-6-astra",
            "kimi": "kimi-code/k3",
            "glm": "zai/glm-5.3-flash",
        }
        for model, selector in expected.items():
            with self.subTest(model=model):
                result = self.run_model("resolve", model)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), f"omp --model={selector}")

    def test_native_harnesses_carry_their_own_flag_spellings(self) -> None:
        # The defaults file names a model or a profile; how each harness spells
        # it stays in the code, and the spellings disagree.
        expected = {
            "claude": "claude",
            "fable": "claude --model fable",
            "codex": "codex",
            "kimi": "kimi -m kimi-code/k3",
            "glm": "opencode -m zai-coding-plan/glm-5.3-flash",
        }
        for alias in ("own", "self"):
            for model, invocation in expected.items():
                with self.subTest(alias=alias, model=model):
                    result = self.run_model("resolve", model, "--harness", alias)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), invocation)

    def test_resume_is_spelled_for_each_harness(self) -> None:
        session_id = "abcxyz123"
        cases = (
            ("claude", "own", f"claude --resume {session_id}"),
            ("codex", "own", f"codex resume {session_id}"),
            (
                "kimi",
                "own",
                f"kimi -m kimi-code/k3 --session {session_id}",
            ),
            (
                "glm",
                "own",
                "opencode -m zai-coding-plan/glm-5.3-flash "
                f"--session {session_id}",
            ),
            (
                "codex",
                "omp",
                f"omp --model=openai-codex/gpt-6-astra --resume {session_id}",
            ),
        )
        for model, harness, invocation in cases:
            with self.subTest(harness=harness):
                result = self.run_model(
                    "resolve", model, "--harness", harness, "--resume", session_id
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), invocation)

    def test_resume_id_pattern_selects_its_native_harness(self) -> None:
        kimi_id = "session_5730bec7-38ff-436c-9124-c1e1ad910662"
        kimi = self.run_model("resolve", "codex", "--resume", kimi_id)
        self.assertEqual(kimi.returncode, 0, kimi.stderr)
        self.assertEqual(kimi.stdout.strip(), f"kimi --session {kimi_id}")

        opencode_id = "ses_fa56499a4ffeUGPrn6w4JSed3K"
        opencode = self.run_model("resolve", "claude", "--resume", opencode_id)
        self.assertEqual(opencode.returncode, 0, opencode.stderr)
        self.assertEqual(opencode.stdout.strip(), f"opencode --session {opencode_id}")

    def test_agentsview_session_ids_are_normalized_for_native_harnesses(self) -> None:
        cases = (
            (
                "codex",
                "codex:01a02c18-042f-7950-8d9a-7d88b50c8cab",
                "codex resume 01a02c18-042f-7950-8d9a-7d88b50c8cab",
            ),
            (
                "claude",
                "opencode:ses_fa56499a4ffeUGPrn6w4JSed3K",
                "opencode --session ses_fa56499a4ffeUGPrn6w4JSed3K",
            ),
            (
                "codex",
                "kimi:wd_weft_b639d530ae70:main:"
                "session_5730bec7-38ff-436c-9124-c1e1ad910662",
                "kimi --session session_5730bec7-38ff-436c-9124-c1e1ad910662",
            ),
        )
        for model, session_id, invocation in cases:
            with self.subTest(session_id=session_id):
                result = self.run_model("resolve", model, "--resume", session_id)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), invocation)

    def test_bare_resume_opens_the_selected_harness_picker(self) -> None:
        default = self.run_model("resolve", "codex", "--resume")
        self.assertEqual(default.returncode, 0, default.stderr)
        self.assertEqual(
            default.stdout.strip(),
            "omp --model=openai-codex/gpt-6-astra --resume",
        )

        native = self.run_model("resolve", "codex", "--harness", "own", "--resume")
        self.assertEqual(native.returncode, 0, native.stderr)
        self.assertEqual(native.stdout.strip(), "codex resume")

    def test_resume_uses_agentsview_to_select_an_ambiguous_harness(self) -> None:
        self.install_agentsview("codex")
        result = self.run_model("resolve", "glm", "--resume", "abcxyz123")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "codex resume abcxyz123")

    def test_unknown_resume_id_uses_the_configured_harness(self) -> None:
        result = self.run_model("resolve", "codex", "--resume", "abcxyz123")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            "omp --model=openai-codex/gpt-6-astra --resume abcxyz123",
        )

    def test_explicit_harness_wins_without_changing_config(self) -> None:
        self.assertEqual(
            self.run_model("default", "set", "codex", "codex").returncode, 0
        )
        result = self.run_model("resolve", "codex", "--harness", "omp", "resume")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(), "omp --model=openai-codex/gpt-6-astra resume"
        )
        self.assertIn('codex = "codex"', self.config().read_text())

    def test_short_harness_option_is_recognized_only_for_a_known_harness(self) -> None:
        selected = self.run_model("resolve", "kimi", "-h", "omp", "--continue")
        self.assertEqual(selected.returncode, 0, selected.stderr)
        self.assertEqual(selected.stdout.strip(), "omp --model=kimi-code/k3 --continue")

        bare_help = self.run_model("resolve", "kimi", "-h")
        self.assertEqual(bare_help.returncode, 0, bare_help.stderr)
        self.assertEqual(bare_help.stdout.strip(), "omp --model=kimi-code/k3 -h")

        native_help = self.run_model("resolve", "kimi", "-h", "topic")
        self.assertEqual(native_help.returncode, 0, native_help.stderr)
        self.assertEqual(
            native_help.stdout.strip(), "omp --model=kimi-code/k3 -h topic"
        )

    def test_long_harness_option_rejects_typos(self) -> None:
        result = self.run_model("resolve", "codex", "--harness", "ompp")
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown harness 'ompp'", result.stderr)

    def test_unsupported_model_harness_pair_is_rejected(self) -> None:
        result = self.run_model("default", "set", "codex", "claude")
        self.assertEqual(result.returncode, 2)
        self.assertIn("codex does not support harness 'claude'", result.stderr)

    def test_launch_executes_the_resolved_command(self) -> None:
        self.install_recorder("omp")
        result = self.run_model("launch", "glm", "--harness=omp", "--continue")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines()[1:],
            ["--model=zai/glm-5.3-flash", "--continue"],
        )

    def test_launch_executes_the_harness_inferred_for_resume(self) -> None:
        self.install_agentsview("codex")
        self.install_recorder("codex")
        result = self.run_model("launch", "glm", "--resume", "abcxyz123")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[1:], ["resume", "abcxyz123"])

    def test_launch_reports_a_missing_harness_executable(self) -> None:
        result = self.run_model("launch", "codex", "--harness", "self")
        self.assertEqual(result.returncode, 2)
        self.assertIn("harness executable 'codex' was not found", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_default_cli_sets_gets_lists_and_resets(self) -> None:
        set_result = self.run_model("default", "set", "codex", "codex")
        self.assertEqual(set_result.returncode, 0, set_result.stderr)
        self.assertEqual(set_result.stdout.strip(), "codex = codex")
        self.assertIn("version = 1", self.config().read_text())

        get_result = self.run_model("default", "get", "codex")
        self.assertEqual(get_result.stdout.strip(), "codex = codex (config)")
        list_result = self.run_model("default", "list")
        self.assertIn("codex   codex     codex      config", list_result.stdout)

        reset_result = self.run_model("default", "reset", "codex")
        self.assertEqual(reset_result.stdout.strip(), "codex = omp (shipped)")
        self.assertNotIn('codex = "', self.config().read_text())

    def test_shipped_defaults_are_valid(self) -> None:
        # Every launch parses this file, so a bad edit breaks every command in
        # every new session. Load it here rather than discovering that there.
        result = self.run_model("config", "check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(REPO / "agent-models.toml"), result.stdout)

    def test_every_shell_function_names_a_shipped_command(self) -> None:
        # The overlay hardcodes the five function names; renaming a command in
        # the defaults file without touching it leaves a function that fails.
        launched = re.findall(
            r"command agent-model launch (\w+)", SHELL_OVERLAY.read_text()
        )
        self.assertTrue(launched)
        for command in launched:
            with self.subTest(command=command):
                result = self.run_model("resolve", command)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_default_without_a_route_is_rejected(self) -> None:
        # The cross-check the three separate tables never had.
        result = self.run_with_defaults(
            "version = 1\n\n"
            "[commands.codex]\n"
            'home = "codex"\n'
            'default = "kimi"\n'
            "routes.codex = {}\n",
            "resolve",
            "codex",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("commands.codex.default", result.stderr)
        self.assertIn("has no route", result.stderr)

    def test_a_field_the_harness_cannot_spell_is_rejected(self) -> None:
        result = self.run_with_defaults(
            "version = 1\n\n"
            "[commands.claude]\n"
            'home = "omp"\n'
            'default = "omp"\n'
            'routes.omp = { profile = "fable" }\n',
            "resolve",
            "claude",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("commands.claude.routes.omp.profile", result.stderr)
        self.assertIn("omp accepts model", result.stderr)

    def test_malformed_config_fails_instead_of_guessing(self) -> None:
        self.config().parent.mkdir(parents=True)
        self.config().write_text('version = 1\n\n[defaults]\ncdoex = "omp"\n')
        result = self.run_model("resolve", "codex")
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown model 'cdoex'", result.stderr)

    def test_config_path_honors_xdg_config_home(self) -> None:
        custom = self.tmp / "xdg"
        self.environment["XDG_CONFIG_HOME"] = str(custom)
        result = self.run_model("config", "path")
        self.assertEqual(
            result.stdout.strip(), str(custom / "agent-models" / "config.toml")
        )


@unittest.skipIf(os.name == "nt", "the shell overlay targets Bash and Zsh")
class ShellOverlayTest(unittest.TestCase):
    def run_shell(
        self, shell: str, interactive: bool
    ) -> subprocess.CompletedProcess[str]:
        if shell.endswith("bash"):
            flags = ["--noprofile", "--norc", "-ic" if interactive else "-c"]
            probe = "declare -F claude fable codex kimi glm"
        else:
            flags = ["-f", "-ic" if interactive else "-c"]
            probe = "whence -w claude fable codex kimi glm"
        return subprocess.run(
            [shell, *flags, f'. "{SHELL_OVERLAY}"; {probe}'],
            capture_output=True,
            check=False,
            env={"HOME": str(Path.home()), "PATH": os.defpath},
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )

    def test_overlay_defines_functions_only_in_interactive_shells(self) -> None:
        for shell in ("/bin/bash", "/bin/zsh"):
            with self.subTest(shell=shell):
                interactive = self.run_shell(shell, True)
                self.assertEqual(interactive.returncode, 0, interactive.stderr)
                for command in ("claude", "fable", "codex", "kimi", "glm"):
                    self.assertIn(command, interactive.stdout)

                noninteractive = self.run_shell(shell, False)
                self.assertNotEqual(noninteractive.returncode, 0)


if __name__ == "__main__":
    unittest.main()
