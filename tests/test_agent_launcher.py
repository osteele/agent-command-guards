"""Tests for the generic agent launcher and its per-agent behaviour."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_agent_model import run_pty

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
        self.environment["AGENT_EPILOGUE_DIR"] = str(self.tmp / "epilogue")
        self.environment.pop("XDG_CONFIG_HOME", None)
        self.environment.pop("ZDOTDIR", None)
        self.environment.pop("AGENT_COMMAND_GUARDS_ACTIVE", None)
        self.environment.pop("OPENAI_API_KEY", None)
        self.environment.pop("ANTHROPIC_API_KEY", None)
        self.environment.pop("AGENT_LAUNCHER_KEEP_API_KEYS", None)
        for variable in ("AGENT_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CLAUDECODE", "GEMINI_CLI"):
            self.environment.pop(variable, None)
        self.install_python()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def install_real(self, name: str) -> Path:
        real = self.real_bin / name
        real.write_text(REPORT_ENVIRONMENT + f'\nprintf launched >> "{self.tmp / "executed"}"\n')
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

    def test_resume_word_as_profile_value_does_not_select_a_conversation(self) -> None:
        """LaunchNewConversation preserves native option values and fresh identity."""
        self.install_real("codex")
        self.install_real("omp")
        self.install_agentsview({OMP_RESUME_ID: "omp"})

        result = self.launch("codex", "--profile", "resume", OMP_RESUME_ID)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"args=--profile resume {OMP_RESUME_ID}", result.stdout)
        self.assertNotEqual(self.session_id(result), OMP_RESUME_ID)

    def test_native_resume_after_a_profile_value_selects_the_actual_identifier(self) -> None:
        """ResumeQueries consumes option values before looking for the native resume marker."""
        self.install_real("codex")
        self.install_agentsview({CODEX_RESUME_ID: "codex"})

        result = self.launch("codex", "--profile", "resume", "resume", CODEX_RESUME_ID)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"args=--profile resume resume {CODEX_RESUME_ID}", result.stdout)
        self.assertEqual(self.session_id(result), CODEX_RESUME_ID)

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

    def test_unresolved_prefixed_name_never_reaches_the_provider(self) -> None:
        """RejectUnresolvedQueryWhenLookupUnavailable applies to prefixed non-IDs."""
        self.install_real("omp")
        result = self.launch("omp", "--resume", "omp:not-a-uuid")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Lookup unavailable", result.stderr)
        self.assertNotIn("args=", result.stdout)

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

    def test_foreign_verified_prefix_switches_harness_and_resume_spelling(self) -> None:
        """SwitchWhenRequestedHarnessHasNoMatch preserves identity and guards."""
        self.install_real("codex")
        self.install_agentsview({CODEX_RESUME_ID: "codex"})
        result = self.launch("omp", "--resume", f"codex:{CODEX_RESUME_ID}", "prompt text")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"args=resume {CODEX_RESUME_ID} prompt text", result.stdout)
        self.assertEqual(self.session_id(result), CODEX_RESUME_ID)
        self.assertIn("guards=1", result.stdout)
        self.assertIn(f"zdotdir={SHELL_INIT}", result.stdout)

    def test_agy_native_prefix_is_normalized(self) -> None:
        """TryExactNativeIdWhenLookupUnavailable preserves invocation harness."""
        self.install_real("agy")
        result = self.launch("agy", "--conversation", f"agy:{AGY_RESUME_ID}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"args=--conversation {AGY_RESUME_ID}", result.stdout)

    # --- resume arguments that are not session ids -------------------------
    #
    # Direct resumes share the model-first resolver, including exact IDs.

    def install_python(self) -> None:
        """Put this suite's interpreter on PATH as `python3`.

        agent-model runs under `#!/usr/bin/env python3` and needs 3.11 for
        tomllib. The bare test PATH reaches the system python, which on macOS
        is older, and a symlink is narrower than adding that interpreter's whole
        directory -- which would also expose the real AgentsView.
        """
        path = self.real_bin / "python3"
        if not path.exists():
            path.symlink_to(sys.executable)

    def install_session_name(self, session_id: str, display_name: str) -> None:
        directory = self.fake_home / ".claude" / "agent-mail" / "session-names"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{session_id}.json").write_text(
            json.dumps(
                {
                    "sessionId": session_id,
                    "assignedAt": "2026-09-01T00:00:00.000Z",
                    "slug": display_name.lower().replace(" ", "-"),
                    "displayName": display_name,
                }
            )
        )

    def install_agentsview(
        self,
        sessions: dict[str, str] | None = None,
        found: list[str] | None = None,
        witness: Path | None = None,
    ) -> None:
        metadata = {
            session_id: json.dumps({"id": session_id, "agent": agent})
            for session_id, agent in (sessions or {}).items()
        }
        matches = json.dumps(
            {
                "matches": [
                    {
                        "session_id": canonical,
                        "agent": canonical.split(":", 1)[0],
                        "timestamp": f"2026-09-{14 + index:02}T23:07:13.647Z",
                    }
                    for index, canonical in enumerate(found or [])
                ]
            }
        )
        script = ["#!/bin/sh"]
        if witness is not None:
            script.append(f"printf '%s\\n' \"$*\" >> {witness}")
        script.append('case "$2" in')
        script.append('  get) case "$3" in')
        for session_id, document in metadata.items():
            script.append(f"      {session_id}) printf '%s' '{document}' ;;")
        script.append('      *) echo "session not found" >&2; exit 1 ;;')
        script.append("    esac ;;")
        script.append(f"  search) printf '%s' '{matches}' ;;")
        script.append("  *) exit 1 ;;")
        script.append("esac")
        path = self.real_bin / "agentsview"
        path.write_text("\n".join(script) + "\n")
        path.chmod(0o755)

    def test_resume_resolves_an_agent_mail_session_name(self) -> None:
        # A name is what a dashboard, a status line, and a peer's mail show;
        # the id behind it is what the agent resolves.
        self.install_real("omp")
        self.install_python()
        self.install_session_name(OMP_RESUME_ID, "Efficient Deer")
        self.install_agentsview({OMP_RESUME_ID: "omp"})
        result = self.launch("omp", "--resume", "Efficient Deer", "-p", "hi")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"args=--resume {OMP_RESUME_ID} -p hi", result.stdout)
        # The resolved id is the session's address, so mail sent to the name
        # still reaches it after the resume.
        self.assertEqual(self.session_id(result), OMP_RESUME_ID)

    def test_resume_resolves_a_name_in_the_attached_form(self) -> None:
        self.install_real("omp")
        self.install_python()
        self.install_session_name(OMP_RESUME_ID, "Efficient Deer")
        self.install_agentsview({OMP_RESUME_ID: "omp"})
        result = self.launch("omp", "--resume=efficient-deer")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"args=--resume={OMP_RESUME_ID}", result.stdout)

    def test_resume_resolves_text_from_a_transcript(self) -> None:
        self.install_real("omp")
        self.install_python()
        self.install_agentsview(found=[f"omp:{OMP_RESUME_ID}"])
        result = self.launch(
            "omp", "--resume", "The review notification confirms the result"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"args=--resume {OMP_RESUME_ID}", result.stdout)
        self.assertEqual(self.session_id(result), OMP_RESUME_ID)

    def test_named_session_switches_to_owning_harness(self) -> None:
        """HarnessOwnership: direct launch resumes only through its owning harness."""
        self.install_real("codex")
        self.install_session_name(CODEX_RESUME_ID, "Efficient Deer")
        self.install_agentsview({CODEX_RESUME_ID: "codex"})
        result = self.launch("omp", "--resume", "Efficient Deer")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"args=resume {CODEX_RESUME_ID}", result.stdout)
        self.assertEqual(self.session_id(result), CODEX_RESUME_ID)

    def test_unmatched_resume_never_executes_the_agent(self) -> None:
        """RejectUnmatchedResume: authoritative name misses fail closed."""
        self.install_real("omp")
        self.install_agentsview()
        result = self.launch("omp", "--resume", "no such session")
        self.assertEqual(result.returncode, 2)
        self.assertIn("No matching conversation", result.stderr)
        self.assertNotIn("args=", result.stdout)

    def test_native_id_authoritatively_absent_is_not_attempted(self) -> None:
        """RejectUnmatchedResume also rejects exact native identifiers."""
        self.install_real("omp")
        self.install_agentsview()
        result = self.launch("omp", "--resume", OMP_RESUME_ID)
        self.assertEqual(result.returncode, 2)
        self.assertIn("No matching conversation", result.stderr)
        self.assertNotIn("args=", result.stdout)

    def test_switch_translates_a_configured_native_model_flag(self) -> None:
        """PrepareResumeModel maps the requested model into the owning harness."""
        self.install_real("omp")
        self.install_agentsview({OMP_RESUME_ID: "omp"})
        result = self.launch("opencode", "-m", "zai-coding-plan/glm-5.3-flash", "-s", OMP_RESUME_ID)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"args=--model=zai/glm-5.3-flash --resume {OMP_RESUME_ID}", result.stdout)
        self.assertNotIn("zai-coding-plan", result.stdout)

    def test_switch_refuses_an_unsupported_native_model_unattended(self) -> None:
        """PrepareResumeModel never drops an unsupported requested model."""
        self.install_real("codex")
        self.install_agentsview({CODEX_RESUME_ID: "codex"})
        result = self.launch("omp", "--model=zai/glm-5.3-flash", "--resume", CODEX_RESUME_ID)
        self.assertEqual(result.returncode, 2)
        self.assertIn("has no route", result.stderr)
        self.assertNotIn("args=", result.stdout)

    def test_nested_codex_identity_preserves_the_outer_receipt(self) -> None:
        """InteractiveTopLevelReceiptsOnly recognizes native Codex nesting before identity reset."""
        self.install_real("omp")
        self.environment["CODEX_THREAD_ID"] = CODEX_RESUME_ID
        self.environment["TERM_SESSION_ID"] = "nested-native"
        card = self.tmp / "epilogue" / "nested-native.card"
        card.parent.mkdir()
        card.write_bytes(b"outer receipt\n")
        status, output = run_pty(
            [str(LAUNCHER_DIR / "omp")], self.environment, [], self.tmp / "executed",
        )
        self.assertEqual(status, 0, output)
        self.assertEqual(card.read_bytes(), b"outer receipt\n")
        self.assertIn("codex_thread=\r\n", output)

    def test_current_exact_conversation_is_excluded_before_identity_reset(self) -> None:
        """CurrentConversationExcluded holds even for direct native-ID invocations."""
        self.install_real("omp")
        self.environment["AGENT_SESSION_ID"] = OMP_RESUME_ID
        result = self.launch("omp", "--resume", OMP_RESUME_ID)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("args=", result.stdout)

    def test_interactive_switch_waits_for_model_choice(self) -> None:
        """SelectCompatibleResumeModel/CancelResumeModelSelection use actual TTY input."""
        self.install_real("codex")
        self.install_agentsview({CODEX_RESUME_ID: "codex"})
        for choice, expected in (("q", 2), ("1", 0)):
            with self.subTest(choice=choice):
                status, output = run_pty(
                    [str(LAUNCHER_DIR / "omp"), "--model=zai/glm-5.3-flash", "--resume", CODEX_RESUME_ID],
                    self.environment, [choice], self.tmp / "executed",
                )
                self.assertEqual(status, expected, output)
                if choice == "q":
                    self.assertNotIn("args=", output)
                else:
                    self.assertIn(f"args=resume {CODEX_RESUME_ID}", output)
                    self.assertIn(f"agent_session={CODEX_RESUME_ID}", output)
                    self.assertIn("guards=1", output)

    def test_interactive_cross_harness_selection_sets_actual_receipt_harness(self) -> None:
        """PromptForCrossHarnessConflict, HarnessOwnership and ReceiptContents share the selection."""
        self.install_real("omp")
        self.install_real("codex")
        self.install_agentsview(
            {OMP_RESUME_ID: "omp", CODEX_RESUME_ID: "codex"},
            found=[f"omp:{OMP_RESUME_ID}", f"codex:{CODEX_RESUME_ID}"],
        )
        self.environment["TERM_SESSION_ID"] = "resume-choice"
        self.environment["AGENT_EPILOGUE_DIR"] = str(self.tmp / "epilogue")
        card = self.tmp / "epilogue" / "resume-choice.card"
        witness = self.tmp / "executed"
        for choice, owner, native_id in (("q", None, None), ("1", "codex", CODEX_RESUME_ID), ("2", "omp", OMP_RESUME_ID)):
            with self.subTest(choice=choice):
                witness.unlink(missing_ok=True)
                card.unlink(missing_ok=True)
                status, output = run_pty(
                    [str(LAUNCHER_DIR / "omp"), "--resume", "remembered line"],
                    self.environment, [choice], witness,
                )
                self.assertEqual(status, 2 if owner is None else 0, output)
                if owner is None:
                    self.assertFalse(witness.exists())
                    self.assertFalse(card.exists())
                else:
                    self.assertTrue(witness.exists())
                    self.assertIn(f"agent_session={native_id}", output)
                    receipt = subprocess.run(
                        [str(REPO / "agent-epilogue"), *card.read_text().splitlines(), "--status", "0"],
                        capture_output=True, text=True, env=self.environment, timeout=15,
                    )
                    self.assertEqual(receipt.returncode, 0, receipt.stderr)
                    spelling = "--resume" if owner == "omp" else "resume"
                    self.assertIn(f"{owner} {spelling} {native_id}", receipt.stdout)

    def test_switch_to_claude_keeps_native_identity(self) -> None:
        """IdentitySeparation holds when the owning harness is the external Claude wrapper."""
        self.install_real("claude")
        self.install_agentsview({OMP_RESUME_ID: "claude"})
        result = self.launch("omp", "--resume", OMP_RESUME_ID)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"args=--resume {OMP_RESUME_ID}", result.stdout)
        self.assertEqual(self.session_id(result), OMP_RESUME_ID)
        self.assertIn("guards=1", result.stdout)

    def test_switch_to_claude_leaves_receipt_to_its_native_wrapper(self) -> None:
        """ReceiptAtMostOncePerRun delegates Claude's receipt to its existing owner."""
        self.install_real("claude")
        self.install_agentsview({OMP_RESUME_ID: "claude"})
        self.environment["TERM_SESSION_ID"] = "claude-owner"

        status, output = run_pty(
            [str(LAUNCHER_DIR / "omp"), "--resume", OMP_RESUME_ID],
            self.environment, [], self.tmp / "executed",
        )

        self.assertEqual(status, 0, output)
        self.assertIn(f"args=--resume {OMP_RESUME_ID}", output)
        self.assertFalse((self.tmp / "epilogue" / "claude-owner.card").exists())

    def test_resume_marker_after_double_dash_is_not_rewritten(self) -> None:
        """LaunchEnvironment preserves ordinary argument semantics after --."""
        self.install_real("omp")
        self.install_agentsview()
        result = self.launch("omp", "--", "--resume", OMP_RESUME_ID)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"args=-- --resume {OMP_RESUME_ID}", result.stdout)
        self.assertNotEqual(self.session_id(result), OMP_RESUME_ID)

    def test_switch_preserves_argument_boundaries_without_shell_evaluation(self) -> None:
        """LaunchEnvironment: owning-harness translation treats arbitrary argv as data."""
        real = self.real_bin / "codex"
        real.write_text(f'#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n')
        real.chmod(0o755)
        self.install_agentsview({CODEX_RESUME_ID: "codex"})
        marker = self.tmp / "should-not-exist"
        prompt = f"two lines\n$(touch {marker})"
        result = self.launch("omp", "--resume", CODEX_RESUME_ID, "--", prompt, "")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["resume", CODEX_RESUME_ID, "--", prompt, ""])
        self.assertFalse(marker.exists())

    def test_a_picker_selector_is_not_treated_as_a_name(self) -> None:
        self.install_real("codex")
        self.install_python()
        witness = self.tmp / "agentsview-calls"
        self.install_agentsview(witness=witness)
        result = self.launch("codex", "resume", "--last")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("args=resume --last", result.stdout)
        self.assertFalse(witness.exists())

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
