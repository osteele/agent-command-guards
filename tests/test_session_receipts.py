"""Tests for the exit receipts: name lookup, rendering, and the launcher card.

The receipt's whole job is to name a session that has already exited, so every
test here works the way the renderer does -- against a store on disk, with no
live session anywhere.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import select
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
AGENT_MAIL_NAME = REPO / "agent-mail-name"
AGENT_EPILOGUE = REPO / "agent-epilogue"
AGENT_LAUNCHER = REPO / "agent-launcher"
EPILOGUE_HOOK = REPO / "shell" / "epilogue.zsh"
NATIVE_ID = "01234567-89ab-cdef-0123-456789abcdef"

ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def plain(text: str) -> str:
    return ESCAPE.sub("", text)


class ReceiptTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.store = self.home / ".claude" / "agent-mail" / "session-names"
        self.breadcrumbs = self.store / "by-host-pid"
        self.registry = self.home / ".claude" / "agent-mail" / "registry"
        for directory in (self.store, self.breadcrumbs, self.registry):
            directory.mkdir(parents=True, exist_ok=True)
        self.environment = dict(os.environ)
        self.environment["HOME"] = str(self.home)
        # External lookup must never reach the workstation's live archive.
        self.bin_dir = self.tmp / "bin"
        self.bin_dir.mkdir()
        if os.name == "posix":
            (self.bin_dir / "python3").symlink_to(sys.executable)
        agentsview = self.bin_dir / "agentsview"
        agentsview.write_text("#!/bin/sh\nexit 1\n")
        agentsview.chmod(0o755)
        self.environment["PATH"] = f"{self.bin_dir}:{os.defpath}"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_name(self, session_id: str, display_name: str) -> None:
        """Store a name the way agent-mail does: keyed by sha256 of the id."""
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        (self.store / f"{digest}.json").write_text(
            json.dumps(
                {
                    "sessionId": session_id,
                    "assignedAt": "2026-09-17T03:29:17.630Z",
                    "scheme": "adjective-noun",
                    "slug": display_name.lower().replace(" ", "-"),
                    "displayName": display_name,
                }
            )
        )

    def write_breadcrumb(
        self, host_pid: int, session_id: str, display_name: str, recorded_at: str
    ) -> None:
        (self.breadcrumbs / f"{host_pid}.json").write_text(
            json.dumps(
                {
                    "hostPid": host_pid,
                    "sessionId": session_id,
                    "recordedAt": recorded_at,
                    "slug": display_name.lower().replace(" ", "-"),
                    "displayName": display_name,
                }
            )
        )

    def write_registration(
        self, host_pid: int, session_id: str, name: str = "project-1"
    ) -> None:
        (self.registry / f"{name}-{host_pid}.json").write_text(
            json.dumps({"pid": host_pid + 1, "parentPid": host_pid, "sessionId": session_id})
        )

    def write_native_session(self) -> None:
        directory = self.home / ".omp" / "agent" / "sessions" / "project"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"2026-09-23T00-00-00-000Z_{NATIVE_ID}.jsonl").write_text("{}\n")

    def resolve(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(AGENT_MAIL_NAME), *arguments],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
            env=self.environment,
        )

    def resume_arguments(self, output: str, harness: str) -> list[str]:
        """Execute the displayed shell command against an external fake harness."""
        command = next(
            line.split("resume  ", 1)[1]
            for line in plain(output).splitlines()
            if "resume  " in line
        )
        fake = self.bin_dir / harness
        fake.write_text(
            f"#!{sys.executable}\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n"
        )
        fake.chmod(0o755)
        result = subprocess.run(
            ["bash", "-c", command], env=self.environment, cwd=self.tmp,
            capture_output=True, text=True, timeout=5, check=True,
        )
        return json.loads(result.stdout)

    def render(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(AGENT_EPILOGUE), *arguments],
            capture_output=True,
            check=False,
            text=True,
            env=self.environment,
            timeout=10,
        )


class NameLookupTest(ReceiptTestCase):
    def test_session_id_resolves_to_display_name(self) -> None:
        self.write_name("abc-123", "Flying Cake")
        result = self.resolve("--session-id", "abc-123")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "Flying Cake")

    def test_unknown_session_is_not_an_error(self) -> None:
        """Exit 3, not 1: a session that never attached agent-mail has no name,
        and that must never be what breaks an exit receipt."""
        result = self.resolve("--session-id", "never-seen")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout, "")

    def test_slug_is_used_when_a_record_has_no_display_name(self) -> None:
        digest = hashlib.sha256(b"slug-only").hexdigest()
        (self.store / f"{digest}.json").write_text(json.dumps({"slug": "amber-ember"}))
        self.assertEqual(self.resolve("--session-id", "slug-only").stdout.strip(), "amber-ember")

    def test_malformed_record_reads_as_no_name(self) -> None:
        digest = hashlib.sha256(b"broken").hexdigest()
        (self.store / f"{digest}.json").write_text("{not json")
        self.assertEqual(self.resolve("--session-id", "broken").returncode, 3)

    def test_host_pid_breadcrumb_wins_where_the_launcher_id_misses(self) -> None:
        """The Codex case: agent-mail keyed the name by a thread id the
        launcher never saw, so only the host pid finds it."""
        self.write_breadcrumb(4242, "codex-thread-id", "Nutritious Cucumber", "2026-09-17T04:00:00.000Z")
        result = self.resolve(
            "--session-id", "launcher-minted-id", "--host-pid", "4242"
        )
        self.assertEqual(result.stdout.strip(), "Nutritious Cucumber")

    def test_session_id_is_preferred_over_the_host_pid(self) -> None:
        self.write_name("abc-123", "Flying Cake")
        self.write_breadcrumb(4242, "other", "Wrong Name", "2026-09-17T04:00:00.000Z")
        result = self.resolve("--session-id", "abc-123", "--host-pid", "4242")
        self.assertEqual(result.stdout.strip(), "Flying Cake")

    def test_breadcrumb_older_than_the_launch_is_rejected(self) -> None:
        """Pids are reused. A breadcrumb from an earlier process with this pid
        would otherwise put a stranger's name on the receipt."""
        self.write_breadcrumb(4242, "older", "Stale Name", "2026-09-16T10:00:00.000Z")
        result = self.resolve(
            "--host-pid", "4242", "--not-before", "2026-09-17T04:00:00Z"
        )
        self.assertEqual(result.returncode, 3)

    def test_breadcrumb_within_the_launch_second_is_kept(self) -> None:
        """agent-mail records milliseconds and the launcher does not, and "."
        sorts below "Z" -- comparing the raw strings would reject a breadcrumb
        written in the same second as the launch."""
        self.write_breadcrumb(4242, "same-second", "Flying Cake", "2026-09-17T04:00:00.630Z")
        result = self.resolve(
            "--host-pid", "4242", "--not-before", "2026-09-17T04:00:00Z"
        )
        self.assertEqual(result.stdout.strip(), "Flying Cake")

    def test_live_registry_answers_without_a_breadcrumb(self) -> None:
        self.write_name("registered-session", "Amber Ember")
        self.write_registration(5150, "registered-session")
        result = self.resolve("--host-pid", "5150")
        self.assertEqual(result.stdout.strip(), "Amber Ember")

    def test_stale_breadcrumb_cannot_fall_through_to_old_registry(self) -> None:
        """ReceiptContents: pid reuse cannot attribute a previous run's name."""
        self.write_name("older", "Wrong Session")
        self.write_breadcrumb(4242, "older", "Wrong Session", "2026-09-16T10:00:00Z")
        self.write_registration(4242, "older")
        result = self.resolve(
            "--host-pid", "4242", "--not-before", "2026-09-17T04:00:00Z"
        )
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout, "")

    def test_undated_breadcrumb_does_not_establish_launch_identity(self) -> None:
        """ReceiptContents: a host-pid name requires evidence newer than launch."""
        for recorded_at in ("", "not-a-time"):
            with self.subTest(recorded_at=recorded_at):
                self.write_breadcrumb(4242, "older", "Wrong Session", recorded_at)
                result = self.resolve(
                    "--host-pid", "4242", "--not-before", "2026-09-17T04:00:00Z"
                )
                self.assertEqual(result.returncode, 3)

    def test_invalid_utf8_degrades_to_no_name(self) -> None:
        """ReceiptContents: corrupt name data cannot break the exit receipt."""
        digest = hashlib.sha256(b"broken").hexdigest()
        (self.store / f"{digest}.json").write_bytes(b"\xff")
        result = self.resolve("--session-id", "broken")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stderr, "")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX named pipes")
    def test_stalled_name_store_cannot_hold_the_prompt(self) -> None:
        """ReceiptContents: unavailable name storage has a bounded no-name outcome."""
        digest = hashlib.sha256(b"stalled").hexdigest()
        os.mkfifo(self.store / f"{digest}.json")
        result = self.resolve("--session-id", "stalled")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stderr, "")


class EpilogueRenderTest(ReceiptTestCase):
    def test_names_the_session_and_offers_a_resume_by_name(self) -> None:
        """ShowExitReceipt: a readable name takes precedence over native identity."""
        self.write_native_session()
        self.write_name(NATIVE_ID, "Flying Cake")
        result = self.render(
            "--harness", "omp", "--cwd", "/tmp/my-project",
            "--session-id", NATIVE_ID, "--status", "0",
        )
        output = plain(result.stdout)
        self.assertIn("my-project", output)
        self.assertIn("Flying Cake", output)
        self.assertEqual(self.resume_arguments(result.stdout, "omp"), ["--resume", "Flying Cake"])

    def test_unverified_native_shaped_id_has_no_resume_line(self) -> None:
        """IdentitySeparation: UUID shape does not prove native resumability."""
        result = self.render("--harness", "kimi", "--cwd", "/tmp/p", "--session-id", NATIVE_ID)
        output = plain(result.stdout)
        self.assertIn("(unnamed session)", output)
        self.assertNotIn("resume", output)

    def test_unnamed_verified_native_id_is_runnable(self) -> None:
        """ShowExitReceipt, IdentitySeparation: exact positive evidence permits ID fallback."""
        self.write_native_session()
        result = self.render("--harness", "omp", "--session-id", NATIVE_ID)
        self.assertEqual(self.resume_arguments(result.stdout, "omp"), ["--resume", NATIVE_ID])

    def test_native_id_owned_by_other_harness_is_not_offered(self) -> None:
        """ReceiptContents: evidence for another harness cannot authorize this resume command."""
        self.write_native_session()
        result = self.render("--harness", "kimi", "--session-id", NATIVE_ID)
        self.assertNotIn("resume  ", plain(result.stdout))

    def test_resume_spelling_follows_the_harness(self) -> None:
        """ReceiptContents: displayed commands preserve literal names and owning syntax."""
        name = "Flying 'Cake' \"$(touch injected)\" `echo nope` $HOME ; *"
        self.write_name("abc-123", name)
        for harness, flag in (
            ("kimi", "--resume"),
            ("omp", "--resume"),
            ("codex", "resume"),
            ("opencode", "--session"),
            ("agy", "--conversation"),
        ):
            with self.subTest(harness=harness):
                result = self.render(
                    "--harness", harness, "--cwd", "/tmp/p", "--session-id", "abc-123"
                )
                self.assertEqual(self.resume_arguments(result.stdout, harness), [flag, name])
                self.assertFalse((self.tmp / "injected").exists())

    def test_exit_status_becomes_how_it_ended(self) -> None:
        """ReceiptContents: report process outcome, never claim task completion."""
        for status, expected in (
            ("0", "exited normally"),
            ("1", "exited with status 1"),
            ("130", "interrupted (Ctrl-C)"),
            ("137", "killed (SIGKILL)"),
            ("143", "terminated (SIGTERM)"),
        ):
            with self.subTest(status=status):
                result = self.render(
                    "--harness", "kimi", "--cwd", "/tmp/p", "--status", status
                )
                self.assertIn(expected, plain(result.stdout))

    def test_a_harness_is_required(self) -> None:
        self.assertEqual(self.render("--cwd", "/tmp/p").returncode, 2)


@unittest.skipUnless(os.name == "posix", "receipt launch requires POSIX terminals")
class LauncherCardTest(ReceiptTestCase):
    def launch(
        self, harness: str, *arguments: str, input_tty: bool = True,
        output_tty: bool = True, **overrides: str,
    ) -> tuple[Path, subprocess.CompletedProcess[str]]:
        fake = self.bin_dir / harness
        fake.write_text("#!/bin/bash\nexit 0\n")
        fake.chmod(0o755)
        link = self.tmp / harness
        if not link.exists():
            link.symlink_to(AGENT_LAUNCHER)
        cards = self.tmp / "cards"
        environment = dict(self.environment)
        environment["AGENT_EPILOGUE_DIR"] = str(cards)
        environment["TERM_SESSION_ID"] = "w1t1p0_TEST"
        for marker in (
            "AGENT_COMMAND_GUARDS_ACTIVE", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID",
            "AGENT_SESSION_ID", "CODEX_THREAD_ID", "GEMINI_CLI",
        ):
            environment.pop(marker, None)
        environment.update(overrides)
        master, slave = os.openpty()
        try:
            result = subprocess.run(
                [str(link), *arguments],
                stdin=slave if input_tty else subprocess.DEVNULL,
                stdout=slave if output_tty else subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, env=environment,
                cwd=str(self.tmp), timeout=15, check=False,
            )
        finally:
            os.close(slave)
            os.close(master)
        self.assertEqual(result.returncode, 0, result.stderr)
        return cards / "w1t1p0_TEST.card", result

    def test_top_level_launch_renders_project_harness_and_outcome(self) -> None:
        """ShowExitReceipt, ShellReceipt: the actual launch yields usable receipt context."""
        card, _ = self.launch("kimi")
        result = self.render(*card.read_text().splitlines(), "--status", "7")
        output = plain(result.stdout)
        self.assertIn(self.tmp.name, output)
        self.assertIn("kimi", output)
        self.assertIn("status 7", output)
        self.assertRegex(output, r"\d+[smh]")

    def test_nested_launch_preserves_outer_receipt(self) -> None:
        """InteractiveTopLevelReceiptsOnly: a nested launch cannot replace the outer card."""
        card, _ = self.launch("kimi")
        outer = card.read_bytes()
        nested, _ = self.launch("codex", AGENT_COMMAND_GUARDS_ACTIVE="1")
        self.assertEqual(nested.read_bytes(), outer)

    def test_noninteractive_launch_publishes_no_card(self) -> None:
        """InteractiveTopLevelReceiptsOnly: both input and output must be interactive."""
        for input_tty, output_tty in ((False, False), (False, True), (True, False)):
            with self.subTest(input_tty=input_tty, output_tty=output_tty):
                card, _ = self.launch("kimi", input_tty=input_tty, output_tty=output_tty)
                self.assertFalse(card.exists())

    def test_headless_mode_with_terminal_publishes_no_card(self) -> None:
        """InteractiveTopLevelReceiptsOnly: a PTY does not make headless execution interactive."""
        for harness, arguments in (
            ("codex", ("exec", "a prompt")), ("omp", ("--print", "a prompt")),
            ("opencode", ("run", "a prompt")), ("kimi", ("--print", "a prompt")),
            ("codex", ("review",)), ("omp", ("--mode", "rpc")),
            ("kimi", ("--help",)),
        ):
            with self.subTest(harness=harness):
                card, _ = self.launch(harness, *arguments)
                self.assertFalse(card.exists())


@unittest.skipUnless(os.name == "posix" and shutil.which("zsh"), "requires interactive Zsh")
class ZshPromptReceiptTest(ReceiptTestCase):
    """Exercise Zsh's actual precmd dispatch, not ordinary function calls."""

    def setUp(self) -> None:
        super().setUp()
        import termios

        self.cards = self.tmp / "cards"
        self.cards.mkdir()
        self.card = self.cards / "receipt-test.card"
        self.environment.update(
            AGENT_EPILOGUE_DIR=str(self.cards), TERM_SESSION_ID="receipt-test",
            TERM="dumb", ZDOTDIR=str(self.home),
        )
        self.master, slave = os.openpty()
        settings = termios.tcgetattr(slave)
        settings[3] &= ~termios.ECHO
        termios.tcsetattr(slave, termios.TCSANOW, settings)
        self.shell = subprocess.Popen(
            ["zsh", "-dfi"], stdin=slave, stdout=slave, stderr=slave,
            env=self.environment, cwd=self.tmp, start_new_session=True,
        )
        os.close(slave)
        self.addCleanup(self.close_shell)
        self.command("PS1='RECEIPT-'\"READY> \"; RPS1=''")
        self.command(
            f"source {shlex.quote(str(EPILOGUE_HOOK))}; "
            f"source {shlex.quote(str(EPILOGUE_HOOK))}; "
            "STARSHIP_CMD_STATUS=42; setopt pipefail"
        )

    def close_shell(self) -> None:
        if self.shell.poll() is None:
            self.shell.terminate()
        try:
            self.shell.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.shell.kill()
            self.shell.wait(timeout=3)
        os.close(self.master)

    def command(self, command: str) -> str:
        os.write(self.master, (command + "\n").encode())
        output = bytearray()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.master], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                break
            try:
                chunk = os.read(self.master, 65536)
            except OSError:
                break
            if not chunk:
                break
            output.extend(chunk)
            rendered = ESCAPE.sub("", output.decode(errors="replace"))
            if rendered.endswith("RECEIPT-READY> "):
                return rendered
        self.fail(f"interactive Zsh did not return its prompt: {output!r}")

    def publish_card(self) -> None:
        self.card.write_text("--harness\nkimi\n--cwd\n/tmp/receipt-project\n")

    def test_hook_consumes_once_and_preserves_failed_pipeline(self) -> None:
        """ReceiptAtMostOncePerRun, ReceiptContents: preserve $?/pipestatus across real prompts."""
        self.command(
            'observe() { print -r -- "OBSERVER:$?:${(j:,:)pipestatus}"; }; '
            'precmd_functions=(observe _agent_epilogue)'
        )
        self.publish_card()
        first = self.command("(exit 7) | (exit 0)")
        self.assertIn("status 7", plain(first))
        self.assertIn("OBSERVER:7:7,0", first)
        self.assertEqual(plain(first).count("receipt-project"), 1)
        self.assertFalse(self.card.exists())
        second = self.command('print -r -- "AFTER:$?:${(j:,:)pipestatus}"')
        self.assertIn("AFTER:7:7,0", second)
        self.assertNotIn("receipt-project", second)

    def test_failed_renderer_cannot_change_status_or_reprint_card(self) -> None:
        """ReceiptAtMostOncePerRun, ReceiptContents: renderer failure is not command failure."""
        self.command("_AGENT_EPILOGUE_RENDERER=/usr/bin/false")
        self.publish_card()
        self.command("(exit 0) | (exit 9)")
        self.assertFalse(self.card.exists())
        output = self.command('print -r -- "AFTER:$?:${(j:,:)pipestatus}"')
        self.assertIn("AFTER:9:0,9", output)
        self.assertNotIn("receipt-project", output)

    def test_successful_exit_receipt_leaves_successful_pipeline(self) -> None:
        """ReceiptContents: a normal process exit remains success, not a task-completion claim."""
        self.publish_card()
        first = self.command("(exit 0) | (exit 0)")
        self.assertIn("exited normally", plain(first))
        output = self.command('print -r -- "AFTER:$?:${(j:,:)pipestatus}"')
        self.assertIn("AFTER:0:0,0", output)


if __name__ == "__main__":
    unittest.main()
