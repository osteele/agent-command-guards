"""Tests for model-first interactive command dispatch."""

from __future__ import annotations

import json
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
        for alias in ("own", "self", "."):
            for model, invocation in expected.items():
                for option in ("--harness", "-h"):
                    with self.subTest(alias=alias, model=model, option=option):
                        result = self.run_model("resolve", model, option, alias)
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

    # --- resume arguments that are not session ids -------------------------
    #
    # A session name and a line of transcript text both name a session a person
    # can see without being able to paste its id.

    def write_session_name(
        self,
        session_id: str,
        display_name: str,
        assigned_at: str = "2026-09-01T00:00:00.000Z",
    ) -> None:
        """Add one record to the agent-mail session-name store this HOME has."""
        directory = self.home / ".claude" / "agent-mail" / "session-names"
        directory.mkdir(parents=True, exist_ok=True)
        slug = display_name.lower().replace(" ", "-")
        (directory / f"{session_id}.json").write_text(
            json.dumps(
                {
                    "sessionId": session_id,
                    "assignedAt": assigned_at,
                    "scheme": "adjective-noun",
                    "slug": slug,
                    "displayName": display_name,
                }
            )
        )

    def install_agentsview_router(
        self,
        sessions: dict[str, str] | None = None,
        found: list[tuple[str, str]] | None = None,
        witness: Path | None = None,
    ) -> None:
        """A fake AgentsView: `sessions` maps id to agent, `found` is a search.

        Each entry of `found` is one (canonical id, timestamp) the transcript
        search reports. `witness` records every call, so a test can assert the
        lookup was never reached at all.
        """
        metadata = {
            session_id: json.dumps(
                {"id": session_id, "agent": agent, "project": "p", "started_at": "2026-09-01T00:00:00Z"}
            )
            for session_id, agent in (sessions or {}).items()
        }
        matches = json.dumps(
            {
                "matches": [
                    {
                        "session_id": canonical,
                        "project": "p",
                        "agent": canonical.split(":", 1)[0],
                        "timestamp": timestamp,
                    }
                    for canonical, timestamp in (found or [])
                ],
                "next_cursor": 0,
            }
        )
        script = ["#!/bin/sh"]
        if witness is not None:
            script.append(f'printf \'%s\\n\' "$*" >> {witness}')
        script.append('case "$2" in')
        script.append("  get)")
        script.append('    case "$3" in')
        for session_id, document in metadata.items():
            script.append(f"      {session_id}) printf '%s' '{document}' ;;")
        script.append('      *) echo "fatal: session $3 not found" >&2; exit 1 ;;')
        script.append("    esac ;;")
        script.append(f"  search) printf '%s' '{matches}' ;;")
        script.append("  *) exit 1 ;;")
        script.append("esac")
        path = self.bin_dir / "agentsview"
        path.write_text("\n".join(script) + "\n")
        path.chmod(0o755)

    def test_resume_accepts_an_agent_mail_session_name(self) -> None:
        session_id = "9a1d4f7c-2b6e-4c11-9f3a-5d8e0c2b7a44"
        self.write_session_name(session_id, "Efficient Deer")
        self.install_agentsview_router({session_id: "codex"})
        for spelling in ("Efficient Deer", "efficient deer", "efficient-deer"):
            with self.subTest(spelling=spelling):
                result = self.run_model("resolve", "glm", "--resume", spelling)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), f"codex resume {session_id}")

    def test_resume_accepts_the_project_qualified_full_name(self) -> None:
        session_id = "9a1d4f7c-2b6e-4c11-9f3a-5d8e0c2b7a44"
        self.write_session_name(session_id, "Efficient Deer")
        self.install_agentsview_router({session_id: "codex"})
        result = self.run_model("resolve", "glm", "--resume", "augur-efficient-deer")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"codex resume {session_id}")

    def test_resume_accepts_text_from_a_transcript(self) -> None:
        session_id = "01a0886b-41a8-7482-a361-86cff36a387f"
        self.install_agentsview_router(
            found=[(f"omp:{session_id}", "2026-09-14T23:07:13.647Z")]
        )
        result = self.run_model(
            "resolve", "glm", "--resume", "the already-verified result"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            f"omp --model=zai/glm-5.3-flash --resume {session_id}",
        )

    def test_transcript_text_selects_the_harness_that_owns_the_session(self) -> None:
        session_id = "01a02c18-042f-7950-8d9a-7d88b50c8cab"
        self.install_agentsview_router(
            found=[(f"codex:{session_id}", "2026-09-14T23:07:13.647Z")]
        )
        result = self.run_model("resolve", "glm", "--resume", "some remembered line")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"codex resume {session_id}")

    def test_a_name_naming_several_sessions_takes_the_newest(self) -> None:
        older = "11111111-1111-4111-8111-111111111111"
        newer = "22222222-2222-4222-8222-222222222222"
        self.write_session_name(older, "Noble Ember", "2026-08-01T00:00:00.000Z")
        self.write_session_name(newer, "Noble Ember", "2026-09-01T00:00:00.000Z")
        self.install_agentsview_router({older: "codex", newer: "codex"})
        result = self.run_model("resolve", "glm", "--resume", "Noble Ember")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"codex resume {newer}")
        self.assertIn("2 sessions match 'Noble Ember'", result.stderr)
        self.assertIn(older, result.stderr)

    def test_a_name_prefers_a_session_of_the_requested_harness(self) -> None:
        # An explicit harness narrows a collision: the newest match loses to the
        # one the user can actually resume with the harness they named.
        opencode_session = "11111111-1111-4111-8111-111111111111"
        kimi_session = "22222222-2222-4222-8222-222222222222"
        self.write_session_name(opencode_session, "Noble Ember", "2026-08-01T00:00:00.000Z")
        self.write_session_name(kimi_session, "Noble Ember", "2026-09-01T00:00:00.000Z")
        self.install_agentsview_router(
            {opencode_session: "opencode", kimi_session: "kimi"}
        )
        result = self.run_model(
            "resolve", "glm", "--harness", "opencode", "--resume", "Noble Ember"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            f"opencode -m zai-coding-plan/glm-5.3-flash --session {opencode_session}",
        )

    def test_resume_never_resolves_to_the_session_doing_the_asking(self) -> None:
        # The phrase a person types to find an old session lands in the
        # transcript of the session they type it in, which the index then finds.
        current = "33333333-3333-4333-8333-333333333333"
        other = "44444444-4444-4444-8444-444444444444"
        self.environment["CLAUDE_CODE_SESSION_ID"] = current
        self.install_agentsview_router(
            found=[
                (f"codex:{current}", "2026-09-16T00:00:00.000Z"),
                (f"codex:{other}", "2026-09-10T00:00:00.000Z"),
            ]
        )
        result = self.run_model("resolve", "glm", "--resume", "a remembered line")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"codex resume {other}")

    def test_an_unresolvable_resume_argument_reaches_the_harness_as_typed(self) -> None:
        self.install_agentsview_router()
        result = self.run_model("resolve", "glm", "--resume", "no such session")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            "omp --model=zai/glm-5.3-flash --resume 'no such session'",
        )

    def test_a_native_session_id_is_never_looked_up(self) -> None:
        # The lookup is the exception, not the path: an id the harness resolves
        # itself must not cost a subprocess on every resume.
        witness = self.tmp / "agentsview-calls"
        self.install_agentsview_router(witness=witness)
        session_id = "ses_fa56499a4ffeUGPrn6w4JSed3K"
        result = self.run_model("resolve", "claude", "--resume", session_id)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"opencode --session {session_id}")
        self.assertFalse(witness.exists(), witness.read_text() if witness.exists() else "")

    def test_resolve_session_reports_the_id_harness_and_resume_spelling(self) -> None:
        session_id = "9a1d4f7c-2b6e-4c11-9f3a-5d8e0c2b7a44"
        self.write_session_name(session_id, "Efficient Deer")
        self.install_agentsview_router({session_id: "kimi"})
        result = self.run_model("resolve-session", "Efficient Deer")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(), [session_id, "kimi", "kimi --session"]
        )

    def test_resolve_session_reports_no_match_without_failing(self) -> None:
        self.install_agentsview_router()
        result = self.run_model("resolve-session", "no such session")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout, "")

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
