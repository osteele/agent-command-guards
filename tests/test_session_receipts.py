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
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
AGENT_MAIL_NAME = REPO / "agent-mail-name"
AGENT_EPILOGUE = REPO / "agent-epilogue"
AGENT_LAUNCHER = REPO / "agent-launcher"

ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


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

    def resolve(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(AGENT_MAIL_NAME), *arguments],
            capture_output=True,
            check=False,
            text=True,
            env=self.environment,
        )

    def render(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(AGENT_EPILOGUE), *arguments],
            capture_output=True,
            check=False,
            text=True,
            env=self.environment,
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


class EpilogueRenderTest(ReceiptTestCase):
    def test_names_the_session_and_offers_a_resume_by_name(self) -> None:
        """The name, not the id: the launcher's AGENT_SESSION_ID is its own
        bookkeeping, and the harness resolves only its native id."""
        self.write_name("abc-123", "Flying Cake")
        result = self.render(
            "--harness", "kimi", "--cwd", "/tmp/my-project",
            "--session-id", "abc-123", "--status", "0",
        )
        output = plain(result.stdout)
        self.assertIn("my-project", output)
        self.assertIn("Flying Cake", output)
        self.assertIn('kimi --resume "Flying Cake"', output)

    def test_unnamed_session_renders_without_a_resume_line(self) -> None:
        result = self.render("--harness", "kimi", "--cwd", "/tmp/p", "--session-id", "x")
        output = plain(result.stdout)
        self.assertIn("(unnamed session)", output)
        self.assertNotIn("resume", output)

    def test_resume_spelling_follows_the_harness(self) -> None:
        self.write_name("abc-123", "Flying Cake")
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
                self.assertIn(f'{harness} {flag} "Flying Cake"', plain(result.stdout))

    def test_exit_status_becomes_how_it_ended(self) -> None:
        for status, expected in (
            ("0", "exited normally"),
            ("1", "crashed (status 1)"),
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


class LauncherCardTest(ReceiptTestCase):
    """The card the launcher leaves for the shell to render.

    The launcher execs its agent, so the card is the only thing that survives
    the launch -- these tests run it with a fake agent binary and read what it
    wrote on the way past.
    """

    def launch(self, harness: str, **overrides: str) -> tuple[Path, subprocess.CompletedProcess[str]]:
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir(exist_ok=True)
        fake = bin_dir / harness
        fake.write_text("#!/bin/bash\nexit 0\n")
        fake.chmod(0o755)
        link = self.tmp / harness
        if not link.exists():
            link.symlink_to(AGENT_LAUNCHER)
        cards = self.tmp / "cards"
        environment = dict(self.environment)
        environment["PATH"] = f"{bin_dir}:{os.defpath}"
        environment["AGENT_EPILOGUE_DIR"] = str(cards)
        environment["TERM_SESSION_ID"] = "w1t1p0_TEST"
        for marker in (
            "AGENT_COMMAND_GUARDS_ACTIVE",
            "CLAUDECODE",
            "CLAUDE_CODE_SESSION_ID",
            "AGENT_SESSION_ID",
            "GEMINI_CLI",
        ):
            environment.pop(marker, None)
        environment.update(overrides)
        # A pty, because the launcher writes a card only for a real terminal:
        # a scripted invocation has no prompt to print one at.
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import pty,sys;sys.exit(pty.spawn(sys.argv[1:]))",
                str(link),
            ],
            capture_output=True,
            check=False,
            text=True,
            env=environment,
            cwd=str(self.tmp),
        )
        return cards / "w1t1p0_TEST.card", result

    def test_card_carries_the_launch_facts(self) -> None:
        card, _ = self.launch("kimi")
        self.assertTrue(card.exists(), "launcher wrote no card")
        fields = card.read_text().splitlines()
        self.assertEqual(fields[0], "--harness")
        self.assertEqual(fields[1], "kimi")
        pairs = dict(zip(fields[::2], fields[1::2]))
        # macOS hands a process the resolved /private form of a temp path.
        self.assertEqual(Path(pairs["--cwd"]).resolve(), self.tmp.resolve())
        self.assertTrue(pairs["--session-id"])
        self.assertTrue(pairs["--host-pid"].isdigit())
        self.assertTrue(pairs["--started"].isdigit())
        self.assertRegex(pairs["--recorded-at"], r"^\d{4}-\d{2}-\d{2}T")

    def test_a_nested_launch_leaves_no_card(self) -> None:
        """One terminal, one card. An agent started inside another agent's
        shell would otherwise overwrite the card its host is waiting on."""
        card, _ = self.launch("kimi", AGENT_COMMAND_GUARDS_ACTIVE="1")
        self.assertFalse(card.exists())

    def test_the_card_renders(self) -> None:
        card, _ = self.launch("kimi")
        arguments = card.read_text().splitlines()
        result = self.render(*arguments, "--status", "0")
        self.assertIn("exited normally", plain(result.stdout))


if __name__ == "__main__":
    unittest.main()
