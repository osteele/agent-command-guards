"""Tests for model-first interactive command dispatch."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
AGENT_MODEL = REPO / "launchers" / "agent-model"
SHELL_OVERLAY = REPO / "shell" / "agent-models.sh"

SESSION_ID = "01a02c18-042f-7950-8d9a-7d88b50c8cab"
OTHER_ID = "11111111-1111-4111-8111-111111111111"


def run_pty(
    arguments: list[str], environment: dict[str, str], answers: list[str],
    witness: Path,
) -> tuple[int, str]:
    """Answer real terminal prompts with a bounded deadline and no child before selection."""
    import errno
    import pty
    import select
    import time

    master, slave = pty.openpty()
    process = subprocess.Popen(arguments, stdin=slave, stdout=slave, stderr=slave, env=environment)
    os.close(slave)
    transcript = b""
    answered = 0
    deadline = time.monotonic() + 15
    try:
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    block = os.read(master, 65536)
                except OSError as error:
                    if error.errno == errno.EIO:
                        break
                    raise
                if not block:
                    break
                transcript += block
                prompts = transcript.count(b"Choose a number, or q to cancel:")
                if prompts > answered and answered < len(answers):
                    if witness.exists():
                        raise AssertionError("harness executed before selection completed")
                    os.write(master, (answers[answered] + "\n").encode())
                    answered += 1
            elif process.poll() is not None:
                break
        else:
            raise AssertionError(f"terminal interaction timed out: {transcript!r}")
        status = process.wait(timeout=3)
        if answered != len(answers):
            raise AssertionError(f"expected {len(answers)} prompts, observed {answered}: {transcript!r}")
        return status, transcript.decode(errors="replace")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        os.close(master)



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
        for variable in ("AGENT_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):
            self.environment.pop(variable, None)
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
        path.write_text(f'#!/bin/sh\nprintf "%s\\n" "$0" "$@"\nprintf launched >> "{self.tmp / "executed"}"\n')
        path.chmod(0o755)

    def install_agentsview(self, agent: str) -> None:
        path = self.bin_dir / "agentsview"
        path.write_text(
            f"#!/bin/sh\nprintf '%s\\n' '{{\"id\":\"{SESSION_ID}\",\"agent\":\"{agent}\"}}'\n"
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


    def test_native_harnesses_carry_their_own_flag_spellings(self) -> None:
        """LaunchNewConversation translates configured routes and native-harness aliases."""
        defaults = """version = 1
[commands.selected]
home = "claude"
default = "omp"
[commands.selected.routes.claude]
model = "selected-model"
[commands.selected.routes.codex]
[commands.selected.routes.kimi]
model = "selected-model"
[commands.selected.routes.opencode]
model = "selected-model"
[commands.selected.routes.omp]
model = "provider/selected-model"
"""
        expected = {
            "claude": "claude --model selected-model",
            "codex": "codex",
            "kimi": "kimi -m selected-model",
            "opencode": "opencode -m selected-model",
            "omp": "omp --model=provider/selected-model",
            "own": "claude --model selected-model",
            "self": "claude --model selected-model",
            ".": "claude --model selected-model",
        }
        for harness, invocation in expected.items():
            with self.subTest(harness=harness):
                result = self.run_with_defaults(
                    defaults, "resolve", "selected", "--harness", harness,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), invocation)

    def test_resume_is_spelled_for_each_harness(self) -> None:
        """ResumeSelectedConversation: translate verified IDs into native syntax."""
        session_id = SESSION_ID
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
                owner = {"claude": "claude", "codex": "codex", "kimi": "kimi", "glm": "opencode"}[model] if harness == "own" else harness
                self.install_agentsview_router({session_id: owner})
                result = self.run_model(
                    "resolve", model, "--harness", harness, "--resume", session_id
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), invocation)

    def test_native_id_shape_does_not_claim_ownership_during_an_outage(self) -> None:
        """TryExactNativeIdWhenLookupUnavailable uses the invocation harness."""
        for session_id in ("session_5730bec7-38ff-436c-9124-c1e1ad910662", "ses_fa56499a4ffeUGPrn6w4JSed3K"):
            result = self.run_model("resolve", "codex", "--resume", session_id)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), f"omp --model=openai-codex/gpt-6-astra --resume {session_id}")
            self.assertIn("Lookup unavailable", result.stderr)

    def test_agentsview_session_ids_are_normalized_for_native_harnesses(self) -> None:
        cases = (
            (
                "codex",
                "codex:01a02c18-042f-7950-8d9a-7d88b50c8cab",
                "codex resume 01a02c18-042f-7950-8d9a-7d88b50c8cab",
            ),
            (
                "glm",
                "opencode:ses_fa56499a4ffeUGPrn6w4JSed3K",
                "opencode -m zai-coding-plan/glm-5.3-flash --session ses_fa56499a4ffeUGPrn6w4JSed3K",
            ),
            (
                "kimi",
                "kimi:wd_weft_b639d530ae70:main:"
                "session_5730bec7-38ff-436c-9124-c1e1ad910662",
                "kimi -m kimi-code/k3 --session session_5730bec7-38ff-436c-9124-c1e1ad910662",
            ),
        )
        for model, session_id, invocation in cases:
            with self.subTest(session_id=session_id):
                native_id = session_id.rsplit(":", 1)[-1]
                self.install_agentsview_router({native_id: session_id.split(":", 1)[0]})
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
        """HarnessOwnership: exact positive metadata outranks a configured default."""
        self.install_agentsview("codex")
        result = self.run_model("resolve", "codex", "--resume", SESSION_ID)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"codex resume {SESSION_ID}")

    def test_unknown_native_id_uses_the_configured_harness_when_lookup_is_unavailable(self) -> None:
        """TryExactNativeIdWhenLookupUnavailable does not infer a fresh conversation."""
        result = self.run_model("resolve", "codex", "--resume", SESSION_ID)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"omp --model=openai-codex/gpt-6-astra --resume {SESSION_ID}")

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
        result = self.run_model("launch", "codex", "--resume", SESSION_ID)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[1:], ["resume", SESSION_ID])

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
        indexed: list[tuple[str, str, str]] | None = None,
    ) -> None:
        """A fake AgentsView: `sessions` maps id to agent, `found` is a search.

        Each entry of `found` is one (canonical id, timestamp) the transcript
        search reports. Each entry of `indexed` is one (canonical id,
        started_at, cwd) that `session list` reports -- the sessions a name
        recorded against a launcher-minted id is joined against. `witness`
        records every call, so a test can assert the lookup was never reached at
        all.
        """
        listed = json.dumps(
            {
                "sessions": [
                    {"id": canonical, "started_at": started, "cwd": cwd, "agent": canonical.split(":", 1)[0]}
                    for canonical, started, cwd in (indexed or [])
                ],
                "total": len(indexed or []),
            }
        )
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
        script.append(f"  list) printf '%s' '{listed}' ;;")
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
                result = self.run_model("resolve", "codex", "--resume", spelling)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), f"codex resume {session_id}")

    def test_resume_accepts_the_project_qualified_full_name(self) -> None:
        session_id = "9a1d4f7c-2b6e-4c11-9f3a-5d8e0c2b7a44"
        self.write_session_name(session_id, "Efficient Deer")
        self.install_agentsview_router({session_id: "codex"})
        result = self.run_model("resolve", "codex", "--resume", "augur-efficient-deer")
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
        result = self.run_model("resolve", "codex", "--resume", "some remembered line")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"codex resume {session_id}")

    def test_a_name_naming_several_sessions_takes_the_newest(self) -> None:
        older = "11111111-1111-4111-8111-111111111111"
        newer = "22222222-2222-4222-8222-222222222222"
        self.write_session_name(older, "Noble Ember", "2026-08-01T00:00:00.000Z")
        self.write_session_name(newer, "Noble Ember", "2026-09-01T00:00:00.000Z")
        self.install_agentsview_router({older: "codex", newer: "codex"})
        result = self.run_model("resolve", "codex", "--resume", "Noble Ember")
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
        result = self.run_model("resolve", "codex", "--resume", "a remembered line")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"codex resume {other}")

    def test_unmatched_resume_stops_without_executing_a_harness(self) -> None:
        """RejectUnmatchedResume: a conclusive query miss never launches a child."""
        self.install_agentsview_router()
        self.install_recorder("omp")
        result = self.run_model("launch", "glm", "--resume", "no such session")
        self.assertEqual(result.returncode, 2)
        self.assertIn("No matching conversation", result.stderr)
        self.assertFalse((self.tmp / "executed").exists())

    def test_authoritative_native_id_miss_is_rejected(self) -> None:
        """RejectUnmatchedResume takes precedence over exact-ID fallback."""
        self.install_agentsview_router()
        self.install_recorder("omp")
        result = self.run_model("launch", "codex", "--resume", SESSION_ID)
        self.assertEqual(result.returncode, 2)
        self.assertIn("No matching conversation", result.stderr)
        self.assertFalse((self.tmp / "executed").exists())

    def write_claude_transcript(self, session_id: str, project: str = "p") -> Path:
        """A transcript file named after the session, as `--resume` expects."""
        directory = self.home / ".claude" / "projects" / project
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{session_id}.jsonl"
        path.write_text("{}\n")
        return path

    def test_a_name_whose_session_no_longer_opens_resolves_to_nothing(self) -> None:
        # Claude Code mints a fresh id for each run of a resumed conversation
        # and appends to the original transcript, so the run's id names no
        # file. agent-mail named that run, so its name outlives the only id it
        # points at -- and resuming it answered "No conversation found".
        session_id = "b1e9a6a9-3d14-41a4-86c9-d7ad9c91cb10"
        self.write_session_name(session_id, "Nutritious Cucumber")
        self.install_agentsview_router()
        result = self.run_model("resolve-session", "Nutritious Cucumber")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout.strip(), "")

    def test_a_named_session_with_a_transcript_costs_no_lookup(self) -> None:
        # A transcript in the harness's own store answers both questions a match
        # raises -- that it opens, and which harness opens it -- so a resume
        # whose id is already native never reaches AgentsView at all. AgentsView
        # answers a hit in milliseconds but concludes a miss only after scanning
        # its archive, which is where the launch-time timeouts came from.
        session_id = "9a1d4f7c-2b6e-4c11-9f3a-5d8e0c2b7a44"
        self.write_session_name(session_id, "Efficient Deer")
        self.write_claude_transcript(session_id)
        witness = self.tmp / "agentsview-calls"
        self.install_agentsview_router({session_id: "claude"}, witness=witness)
        result = self.run_model("resolve-session", "Efficient Deer")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(), [session_id, "claude", "claude --resume"]
        )
        calls = witness.read_text().splitlines() if witness.exists() else []
        self.assertEqual(calls, [])

    def write_announced(self, session_id: str, project: str) -> Path:
        """agent-mail's announcement record, where a session's cwd outlives it."""
        directory = self.home / ".claude" / "agent-mail" / "announced"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"typeset-viewer-67c7ac162c-{session_id}.json"
        path.write_text(
            json.dumps({"version": 1, "sessionId": session_id, "project": project})
        )
        return path

    # A name minted against the launcher's own id. Uppercase because uuidgen
    # uppercases and every harness's native id is lowercase, but nothing reads
    # the case -- what makes it unresumable is that no harness store holds it.
    LAUNCHER_ID = "EE1E8456-295B-4BE5-AB37-06D3FC268E8C"

    def test_name_recorded_against_launcher_id_is_not_joined_by_time_or_directory(self) -> None:
        """IdentitySeparation: even a unique close-by session is not exact identity."""
        self.write_session_name(
            self.LAUNCHER_ID, "Gifted Bowl", assigned_at="2026-09-18T00:40:01.720Z"
        )
        self.write_announced(self.LAUNCHER_ID, "/w/typeset-viewer")
        self.install_agentsview_router(indexed=[
            (f"omp:{SESSION_ID}", "2026-09-18T00:40:01.244Z", "/w/typeset-viewer"),
        ])
        result = self.run_model("resolve-session", "Gifted Bowl", "--agent", "omp")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout, "")

    def test_unavailable_lookup_does_not_launch_an_unconfirmed_name(self) -> None:
        """RejectUnresolvedQueryWhenLookupUnavailable rejects stale name records."""
        self.write_session_name(SESSION_ID, "Efficient Deer")
        broken = self.bin_dir / "agentsview"
        broken.write_text("#!/bin/sh\necho 'boom' >&2\nexit 1\n")
        broken.chmod(0o755)
        self.install_recorder("omp")
        result = self.run_model("launch", "codex", "--resume", "Efficient Deer")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Lookup unavailable", result.stderr)
        self.assertNotIn("No matching conversation", result.stderr)
        self.assertFalse((self.tmp / "executed").exists())
        result = self.run_model("resolve-session", "Efficient Deer")
        self.assertEqual(result.returncode, 4)
        self.assertEqual(result.stdout, "")

    def test_verify_session_requires_exact_identity_and_owning_harness(self) -> None:
        """IdentitySeparation: verification cannot return a different native conversation."""
        self.install_agentsview_router({SESSION_ID: "codex"})
        verified = self.run_model("verify-session", SESSION_ID, "--agent", "codex")
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertEqual(verified.stdout.splitlines(), [SESSION_ID, "codex"])
        wrong_owner = self.run_model("verify-session", SESSION_ID, "--agent", "omp")
        self.assertEqual(wrong_owner.returncode, 3)
        self.assertEqual(wrong_owner.stdout, "")
        self.write_session_name(SESSION_ID, "Efficient Deer")
        named = self.run_model("verify-session", "Efficient Deer", "--agent", "codex")
        self.assertEqual(named.returncode, 2)
        self.assertEqual(named.stdout, "")

    def test_verify_session_uses_exact_local_store_before_agentsview(self) -> None:
        """IdentitySeparation: authoritative local identity works during index outages."""
        self.write_claude_transcript(SESSION_ID)
        self.environment["CLAUDE_CODE_SESSION_ID"] = SESSION_ID
        result = self.run_model("verify-session", SESSION_ID, "--agent", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [SESSION_ID, "claude"])

    def test_verify_session_rejects_wrong_metadata_and_partial_filenames(self) -> None:
        """IdentitySeparation: prefixes, nearby files and returned unrelated IDs prove nothing."""
        directory = self.home / ".codex" / "sessions" / "2026" / "09" / "23"
        directory.mkdir(parents=True)
        (directory / f"rollout-2026-09-23-{SESSION_ID}-other.jsonl").write_text("{}\n")
        path = self.bin_dir / "agentsview"
        for record in (
            {"id": OTHER_ID, "agent": "codex"},
            {"agent": "codex"},
            {"id": f"omp:{SESSION_ID}", "agent": "codex"},
            {"id": SESSION_ID, "agent": []},
        ):
            with self.subTest(record=record):
                path.write_text(f"#!/bin/sh\nprintf '%s' '{json.dumps(record)}'\n")
                path.chmod(0o755)
                result = self.run_model("verify-session", SESSION_ID, "--agent", "codex")
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertEqual(result.stdout, "")

    def test_verify_session_absent_or_unavailable_never_prints_an_id(self) -> None:
        """IdentitySeparation: an exact-ID attempt is not positive verification."""
        for installed in (False, True):
            with self.subTest(installed=installed):
                if installed:
                    self.install_agentsview_router()
                result = self.run_model("verify-session", SESSION_ID, "--agent", "codex")
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertEqual(result.stdout, "")

    def test_unavailable_phrase_lookup_is_distinct_from_a_miss(self) -> None:
        """RejectUnresolvedQueryWhenLookupUnavailable never forwards transcript prose."""
        self.install_recorder("omp")
        result = self.run_model("launch", "glm", "--resume", "a remembered line")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Lookup unavailable", result.stderr)
        self.assertNotIn("No matching conversation", result.stderr)
        self.assertFalse((self.tmp / "executed").exists())

    def test_search_selects_newest_and_reports_all_distinct_alternatives(self) -> None:
        """SelectNewestWithoutHarnessPreference/ResumeSelectedConversation deduplicate results."""
        third = "33333333-3333-4333-8333-333333333333"
        self.install_agentsview_router(found=[
            (f"omp:{OTHER_ID}", "2026-09-02T12:00:00Z"),
            (f"codex:{SESSION_ID}", "2026-09-03T12:00:00Z"),
            (f"codex:{SESSION_ID}", "2026-09-01T12:00:00Z"),
            (f"omp:{third}", "2026-09-02T20:00:00+09:00"),
        ])
        result = self.run_model("resolve", "codex", "--resume", "remembered line")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"codex resume {SESSION_ID}")
        self.assertIn(OTHER_ID, result.stderr)
        self.assertIn(third, result.stderr)
        self.assertIn("3 sessions", result.stderr)

    def test_unattended_conflict_prefers_requested_harness_and_reports_foreign_match(self) -> None:
        """PreferRequestedHarness includes cross-harness alternatives in the report."""
        self.install_agentsview_router(found=[
            (f"codex:{SESSION_ID}", "2026-09-03T00:00:00Z"),
            (f"omp:{OTHER_ID}", "2026-09-02T00:00:00Z"),
        ])
        result = self.run_model("resolve", "codex", "--harness", "omp", "--resume", "remembered line")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"omp --model=openai-codex/gpt-6-astra --resume {OTHER_ID}")
        self.assertIn(SESSION_ID, result.stderr)

    def test_explicit_harness_without_match_switches_to_owner(self) -> None:
        """SwitchWhenRequestedHarnessHasNoMatch overrides the explicit invocation harness."""
        self.install_agentsview_router({SESSION_ID: "codex"})
        result = self.run_model("resolve", "codex", "--harness", "omp", "--resume", SESSION_ID)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"codex resume {SESSION_ID}")

    def test_model_route_is_checked_on_the_selected_harness(self) -> None:
        """PrepareResumeModel validates the owner, not a discarded invocation harness."""
        self.install_agentsview_router({SESSION_ID: "omp"})
        result = self.run_model("resolve", "glm", "--harness", "codex", "--resume", SESSION_ID)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"omp --model=zai/glm-5.3-flash --resume {SESSION_ID}")

    def test_unsupported_model_without_switch_is_rejected(self) -> None:
        """PrepareResumeModel's unchanged-harness branch also refuses missing routes."""
        self.install_agentsview_router({SESSION_ID: "codex"})
        self.install_recorder("codex")
        result = self.run_model("launch", "glm", "--harness", "codex", "--resume", SESSION_ID)
        self.assertEqual(result.returncode, 2)
        self.assertIn("has no route", result.stderr)
        self.assertFalse((self.tmp / "executed").exists())

    @unittest.skipIf(os.name == "nt", "terminal choice tests require a POSIX PTY")
    def test_interactive_newest_selection_without_conflict_does_not_prompt(self) -> None:
        """SelectNewestWithoutHarnessPreference/PreferRequestedHarness do not prompt unnecessarily."""
        self.install_agentsview_router(found=[
            (f"codex:{SESSION_ID}", "2026-09-03T00:00:00Z"),
            (f"omp:{OTHER_ID}", "2026-09-02T00:00:00Z"),
        ])
        self.install_recorder("codex")
        witness = self.tmp / "executed"
        for preference in ([], ["--harness", "codex"]):
            with self.subTest(preference=preference):
                witness.unlink(missing_ok=True)
                status, output = run_pty(
                    [sys.executable, str(AGENT_MODEL), "launch", "codex", *preference, "--resume", "remembered line"],
                    self.environment, [], witness,
                )
                self.assertEqual(status, 0, output)
                self.assertTrue(witness.exists())
                self.assertIn(f"\r\nresume\r\n{SESSION_ID}\r\n", output)
                self.assertIn(OTHER_ID, output)

    def test_unsupported_model_after_switch_never_uses_a_default_unattended(self) -> None:
        """PrepareResumeModel rejects a model with no configured route after switching."""
        self.install_agentsview_router({SESSION_ID: "codex"})
        self.install_recorder("codex")
        result = self.run_model("launch", "glm", "--resume", SESSION_ID)
        self.assertEqual(result.returncode, 2)
        self.assertIn("glm", result.stderr)
        self.assertIn("codex", result.stderr)
        self.assertFalse((self.tmp / "executed").exists())

    def test_exact_current_conversation_is_rejected_without_fallback(self) -> None:
        """CurrentConversationExcluded also applies when the caller types its exact ID."""
        self.environment["CODEX_THREAD_ID"] = SESSION_ID
        self.install_recorder("omp")
        result = self.run_model("launch", "codex", "--resume", SESSION_ID)
        self.assertEqual(result.returncode, 2)
        self.assertIn("No matching conversation", result.stderr)
        self.assertFalse((self.tmp / "executed").exists())

    @unittest.skipIf(os.name == "nt", "terminal choice tests require a POSIX PTY")
    def test_interactive_cross_harness_choice_and_cancel(self) -> None:
        """PromptForCrossHarnessConflict/SelectPromptedConversation/CancelConversationSelection."""
        self.install_agentsview_router(found=[
            (f"codex:{SESSION_ID}", "2026-09-03T00:00:00Z"),
            (f"omp:{OTHER_ID}", "2026-09-02T00:00:00Z"),
        ])
        self.install_recorder("codex")
        self.install_recorder("omp")
        witness = self.tmp / "executed"
        for answers, expected in ((["q"], None), (["9", "1"], SESSION_ID), (["2"], OTHER_ID)):
            with self.subTest(answers=answers):
                witness.unlink(missing_ok=True)
                status, output = run_pty(
                    [sys.executable, str(AGENT_MODEL), "launch", "codex", "--harness", "omp", "--resume", "remembered line"],
                    self.environment, answers, witness,
                )
                self.assertEqual(status, 2 if expected is None else 0, output)
                if expected is None:
                    self.assertFalse(witness.exists())
                else:
                    self.assertTrue(witness.exists())
                    self.assertIn(f"\r\n{expected}\r\n", output)

    @unittest.skipIf(os.name == "nt", "terminal choice tests require a POSIX PTY")
    def test_interactive_model_selection_and_cancel(self) -> None:
        """PrepareResumeModel/SelectCompatibleResumeModel/CancelResumeModelSelection."""
        self.install_agentsview_router({SESSION_ID: "codex"})
        self.install_recorder("codex")
        witness = self.tmp / "executed"
        for answers, status_expected in ((["q"], 2), (["1"], 0)):
            with self.subTest(answers=answers):
                witness.unlink(missing_ok=True)
                status, output = run_pty(
                    [sys.executable, str(AGENT_MODEL), "launch", "glm", "--resume", SESSION_ID],
                    self.environment, answers, witness,
                )
                self.assertEqual(status, status_expected, output)
                self.assertIn("glm", output)
                self.assertIn("codex", output)
                self.assertEqual(witness.exists(), status == 0)
                if status == 0:
                    self.assertIn(f"\r\nresume\r\n{SESSION_ID}\r\n", output)

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
