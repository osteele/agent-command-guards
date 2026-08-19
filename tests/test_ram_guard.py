"""Tests for the local resident-memory guard and uv shadow."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SHADOWS = Path(__file__).resolve().parent.parent / "shadows"
GUARD = SHADOWS / "ram-guard"
UV_SHADOW = SHADOWS / "uv"

loader = importlib.machinery.SourceFileLoader("ram_guard", str(GUARD))
spec = importlib.util.spec_from_loader(loader.name, loader)
if spec is None or spec.loader is None:
    raise RuntimeError("could not load ram-guard")
ram_guard = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ram_guard
spec.loader.exec_module(ram_guard)

requires_posix_processes = unittest.skipIf(
    os.name == "nt",
    "ram-guard's process groups and memory monitors are POSIX-only",
)
requires_posix_shell = unittest.skipIf(
    os.name == "nt", "executable shell-script fakes need a POSIX system"
)


class SizeParsingTest(unittest.TestCase):
    def test_binary_sizes(self) -> None:
        self.assertEqual(ram_guard.parse_size("8G"), 8 * 1024**3)
        self.assertEqual(ram_guard.parse_size("512MiB"), 512 * 1024**2)

    def test_rejects_non_finite_sizes(self) -> None:
        for value in ("inf", "infG", "nan", "-infM"):
            with (
                self.subTest(value=value),
                self.assertRaises(ram_guard.argparse.ArgumentTypeError),
            ):
                ram_guard.parse_size(value)


class AvailableMemoryTest(unittest.TestCase):
    def test_parses_macos_memory_pressure(self) -> None:
        output = (
            "The system has 17179869184 (1048576 pages with a page size of 16384).\n"
            "System-wide memory free percentage: 36%\n"
        )
        self.assertEqual(
            ram_guard.parse_memory_pressure_output(output),
            int(17179869184 * 0.36),
        )

    def test_parses_linux_meminfo(self) -> None:
        self.assertEqual(
            ram_guard.parse_meminfo("MemTotal: 100000 kB\nMemAvailable: 42000 kB\n"),
            42000 * 1024,
        )

    def test_dynamic_limit_uses_configured_fraction(self) -> None:
        original_fraction = os.environ.get("LLM_RAM_GUARD_AVAILABLE_FRACTION")
        original_limit = os.environ.get("LLM_RAM_GUARD_LIMIT")
        try:
            with mock.patch.object(
                ram_guard, "available_memory_bytes", return_value=10 * 1024**3
            ):
                os.environ["LLM_RAM_GUARD_AVAILABLE_FRACTION"] = "0.6"
                os.environ.pop("LLM_RAM_GUARD_LIMIT", None)
                limit, source, available = ram_guard.resolve_memory_limit(None)
        finally:
            if original_fraction is None:
                os.environ.pop("LLM_RAM_GUARD_AVAILABLE_FRACTION", None)
            else:
                os.environ["LLM_RAM_GUARD_AVAILABLE_FRACTION"] = original_fraction
            if original_limit is None:
                os.environ.pop("LLM_RAM_GUARD_LIMIT", None)
            else:
                os.environ["LLM_RAM_GUARD_LIMIT"] = original_limit
        self.assertEqual(limit, 6 * 1024**3)
        self.assertEqual(source, "60% of 10.00 GiB available at launch")
        self.assertEqual(available, 10 * 1024**3)


class CeilingAnnouncementTest(unittest.TestCase):
    @staticmethod
    def announce(*, isatty: bool, quiet: str | None) -> bool:
        stderr = mock.Mock()
        stderr.isatty.return_value = isatty
        environment = {k: v for k, v in os.environ.items() if k != "LLM_RAM_GUARD_QUIET"}
        if quiet is not None:
            environment["LLM_RAM_GUARD_QUIET"] = quiet
        with (
            mock.patch.object(ram_guard.sys, "stderr", stderr),
            mock.patch.dict(os.environ, environment, clear=True),
        ):
            return ram_guard.should_announce_ceiling()

    def test_announces_to_a_terminal(self) -> None:
        self.assertTrue(self.announce(isatty=True, quiet=None))

    def test_silent_when_stderr_is_captured(self) -> None:
        # `jj fix` runs the formatter once per file per revision and reports the
        # captured stderr under each filename; a banner there is pure noise.
        self.assertFalse(self.announce(isatty=False, quiet=None))

    def test_quiet_overrides_a_terminal(self) -> None:
        for value in ("1", "true", "YES", "on"):
            with self.subTest(value=value):
                self.assertFalse(self.announce(isatty=True, quiet=value))

    def test_unset_quiet_values_leave_the_terminal_banner(self) -> None:
        for value in ("0", "false", "no", ""):
            with self.subTest(value=value):
                self.assertTrue(self.announce(isatty=True, quiet=value))


class RamGuardIntegrationTest(unittest.TestCase):
    def test_process_inspection_isolated_from_unrelated_memory_use(self) -> None:
        child = mock.Mock(pid=4242)
        child.poll.return_value = 0
        rows = (ram_guard.ProcessRow(4242, 1, 4242, 1024),)

        with (
            mock.patch.object(ram_guard.subprocess, "Popen", return_value=child),
            mock.patch.object(ram_guard, "process_rows", return_value=rows),
            mock.patch.object(
                ram_guard, "available_memory_bytes", return_value=1
            ) as available_memory,
            mock.patch.object(
                ram_guard, "terminate_child", return_value=137
            ) as terminate_child,
        ):
            result = ram_guard.run_guarded(
                ["ignored"],
                limit_bytes=1024**2,
                available_at_launch=2 * 1024**2,
                poll_seconds=0.001,
                term_grace_seconds=0,
            )

        self.assertEqual(result, 0)
        available_memory.assert_not_called()
        terminate_child.assert_not_called()

    def test_transient_inspection_failure_keeps_the_resident_ceiling(self) -> None:
        # A lone slow `ps` must not hand the run to the available-memory floor,
        # which would terminate this tree for memory the rest of the host used.
        child = mock.Mock(pid=4242)
        child.poll.side_effect = [None, 0]
        rows = (ram_guard.ProcessRow(4242, 1, 4242, 1024),)

        with (
            mock.patch.object(ram_guard.subprocess, "Popen", return_value=child),
            mock.patch.object(
                ram_guard,
                "process_rows",
                side_effect=[ram_guard.ProcessInspectionError("slow"), rows],
            ),
            mock.patch.object(
                ram_guard, "available_memory_bytes", return_value=1
            ) as available_memory,
            mock.patch.object(
                ram_guard, "terminate_child", return_value=137
            ) as terminate_child,
        ):
            result = ram_guard.run_guarded(
                ["ignored"],
                limit_bytes=1024**2,
                available_at_launch=2 * 1024**2,
                poll_seconds=0.001,
                term_grace_seconds=0,
            )

        self.assertEqual(result, 0)
        available_memory.assert_not_called()
        terminate_child.assert_not_called()

    def test_recovered_inspection_resumes_the_resident_ceiling(self) -> None:
        child = mock.Mock(pid=4242)
        child.poll.return_value = None
        over_limit = (ram_guard.ProcessRow(4242, 1, 4242, 4 * 1024**2),)
        failures = [ram_guard.ProcessInspectionError("slow")] * (
            ram_guard.INSPECTION_FAILURE_TOLERANCE + 1
        )

        with (
            mock.patch.object(ram_guard.subprocess, "Popen", return_value=child),
            mock.patch.object(
                ram_guard, "process_rows", side_effect=[*failures, over_limit]
            ),
            # Well clear of the floor, so nothing terminates while degraded.
            mock.patch.object(
                ram_guard, "available_memory_bytes", return_value=1024**4
            ) as available_memory,
            mock.patch.object(
                ram_guard, "terminate_child", return_value=137
            ) as terminate_child,
        ):
            result = ram_guard.run_guarded(
                ["ignored"],
                limit_bytes=1024**2,
                available_at_launch=2 * 1024**2,
                poll_seconds=0.001,
                term_grace_seconds=0,
            )

        self.assertEqual(result, 137)
        # The floor was consulted while degraded, then the ceiling took over.
        available_memory.assert_called()
        terminate_child.assert_called_once_with(child, 4242, 0, 137)

    def test_available_memory_floor_is_fallback_for_restricted_sandboxes(
        self,
    ) -> None:
        child = mock.Mock(pid=4242)
        child.poll.return_value = None
        with (
            mock.patch.object(ram_guard.subprocess, "Popen", return_value=child),
            mock.patch.object(
                ram_guard,
                "process_rows",
                side_effect=ram_guard.ProcessInspectionError("restricted"),
            ),
            mock.patch.object(ram_guard, "available_memory_bytes", return_value=1),
            mock.patch.object(
                ram_guard, "terminate_child", return_value=137
            ) as terminate_child,
        ):
            result = ram_guard.run_guarded(
                ["ignored"],
                limit_bytes=1024**2,
                available_at_launch=2 * 1024**2,
                poll_seconds=0.001,
                term_grace_seconds=0,
            )

        self.assertEqual(result, 137)
        terminate_child.assert_called_once_with(child, 4242, 0, 137)

    @requires_posix_processes
    def test_escalation_kill_tolerates_a_recycled_process_group(self) -> None:
        # On a loaded host the guarded group can exit and have its id recycled
        # between the liveness probe and the escalation kill; EPERM then names
        # a group that is no longer ours and must not crash the guard.
        sent: list[int] = []

        def killpg(_pgid: int, sig: int) -> None:
            sent.append(sig)
            if sig == signal.SIGKILL:
                raise PermissionError(1, "Operation not permitted")

        with mock.patch.object(ram_guard.os, "killpg", side_effect=killpg):
            ram_guard.terminate_process_group(4242, 0)

        self.assertEqual(sent, [signal.SIGTERM, signal.SIGKILL])

    @requires_posix_processes
    def test_passes_normal_command_and_mps_defaults(self) -> None:
        code = (
            "import json,os; print(json.dumps({"
            "'active':os.environ.get('LLM_RAM_GUARD_ACTIVE'),"
            "'high':os.environ.get('PYTORCH_MPS_HIGH_WATERMARK_RATIO'),"
            "'low':os.environ.get('PYTORCH_MPS_LOW_WATERMARK_RATIO')}))"
        )
        result = subprocess.run(
            [str(GUARD), "--limit", "128M", "--", sys.executable, "-c", code],
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {"active": "1", "high": "0.7", "low": "0.6"},
        )

    @requires_posix_processes
    def test_terminates_process_tree_above_limit(self) -> None:
        try:
            ram_guard.process_rows()
        except ram_guard.ProcessInspectionError:
            self.skipTest("process-table inspection is unavailable in this sandbox")
        # bytearray() hands back lazily-mapped zero pages, so an untouched
        # allocation leaves the child resident in about 9 MiB and the guard
        # rightly finds nothing above the ceiling. Write a byte per page so the
        # allocation is genuinely resident, the way a runaway job's would be.
        # The child then outlives many poll intervals, since the guard samples
        # by spawning `ps` and a loaded host needs room to take a sample.
        code = (
            "import time;"
            "value = bytearray(80 * 1024 * 1024);"
            "value[::4096] = b'x' * len(value[::4096]);"
            "time.sleep(30)"
        )
        result = subprocess.run(
            [
                str(GUARD),
                "--limit",
                "32M",
                "--poll-seconds",
                "0.05",
                "--term-grace-seconds",
                "0.1",
                "--",
                sys.executable,
                "-c",
                code,
            ],
            capture_output=True,
            check=False,
            text=True,
            # Only a deadlock backstop, not a speed assertion. The guarded run
            # normally finishes well under a second, but interpreter start plus
            # one `ps` poll is wall-clock work; on a loaded host a tight budget
            # turns this into a spurious TimeoutExpired error.
            timeout=120,
        )
        self.assertEqual(result.returncode, 137, result.stderr)
        self.assertTrue(
            "resident-memory limit exceeded" in result.stderr
            or "available-memory reserve reached" in result.stderr,
            result.stderr,
        )

    @requires_posix_processes
    def test_sigterm_to_the_guard_reaches_the_child_group(self) -> None:
        # An agent session that cancels a guarded run signals the guard, not
        # the child; the guard must forward to the child's process group and
        # report the conventional 128+signum exit code.
        try:
            ram_guard.process_rows()
        except ram_guard.ProcessInspectionError:
            self.skipTest("process-table inspection is unavailable in this sandbox")
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "child.pid"
            code = (
                "import os, time; "
                f"open({str(pid_file)!r}, 'w').write(str(os.getpid())); "
                "time.sleep(30)"
            )
            guard = subprocess.Popen(
                [
                    str(GUARD),
                    "--limit",
                    "1G",
                    "--poll-seconds",
                    "0.05",
                    "--",
                    sys.executable,
                    "-c",
                    code,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                # Keep the guard's own signal handling out of this test
                # session's process group.
                start_new_session=True,
            )
            try:
                for _ in range(200):
                    if pid_file.exists():
                        break
                    time.sleep(0.05)
                self.assertTrue(
                    pid_file.exists(), "child never recorded its pid"
                )
                child_pid = int(pid_file.read_text())
                os.kill(guard.pid, signal.SIGTERM)
                stdout, stderr = guard.communicate(timeout=30)
            finally:
                if guard.poll() is None:
                    guard.kill()
                    guard.communicate()

        self.assertEqual(guard.returncode, 143, stderr)
        self.assertNotIn("limit exceeded", stderr)
        # The forwarded signal terminated the guarded child, and the guard
        # reaped it: the pid is gone rather than lingering under a live group.
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)


@requires_posix_shell
class UvShadowIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.shim_bin = self.tmp / "shims"
        self.shim_bin.mkdir()
        self.real_bin = self.tmp / "bin"
        self.real_bin.mkdir()
        dispatcher = self.shim_bin / "mise"
        dispatcher.write_text("#!/bin/sh\nexit 99\n")
        dispatcher.chmod(0o755)
        (self.shim_bin / "uv").symlink_to(dispatcher)
        self.real_uv = self.real_bin / "uv"
        self.real_uv.write_text(
            "#!/bin/sh\n"
            "printf 'args=%s\\n' \"$*\"\n"
            "printf 'active=%s\\n' \"${LLM_RAM_GUARD_ACTIVE:-}\"\n"
            "printf 'high=%s\\n' \"${PYTORCH_MPS_HIGH_WATERMARK_RATIO:-}\"\n"
        )
        self.real_uv.chmod(0o755)
        self.environment = dict(os.environ)
        self.environment["PATH"] = (
            f"{SHADOWS}:{self.shim_bin}:{self.real_bin}:/usr/bin:/bin"
        )
        self.environment["LLM_RAM_GUARD_LIMIT"] = "128M"
        for name in (
            "LLM_RAM_GUARD_ACTIVE",
            "PYTORCH_MPS_HIGH_WATERMARK_RATIO",
            "PYTORCH_MPS_LOW_WATERMARK_RATIO",
        ):
            self.environment.pop(name, None)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_uv_run_is_guarded(self) -> None:
        result = subprocess.run(
            [str(UV_SHADOW), "run", "python", "experiment.py"],
            capture_output=True,
            check=False,
            env=self.environment,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("args=run python experiment.py", result.stdout)
        self.assertIn("active=1", result.stdout)
        self.assertIn("high=0.7", result.stdout)

    def test_uv_run_after_global_options_is_guarded(self) -> None:
        for args in (
            ["--quiet", "run", "python", "experiment.py"],
            ["--directory", "project", "run", "python", "experiment.py"],
            ["--color=never", "run", "python", "experiment.py"],
        ):
            with self.subTest(args=args):
                result = subprocess.run(
                    [str(UV_SHADOW), *args],
                    capture_output=True,
                    check=False,
                    env=self.environment,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("active=1", result.stdout)

    def test_non_run_and_explicit_bypass_are_not_guarded(self) -> None:
        for args, overrides in (
            (["sync"], {}),
            (["--project", "run", "sync"], {}),
            (["tool", "run"], {}),
            (["run", "python", "experiment.py"], {"LLM_RAM_GUARD": "off"}),
        ):
            with self.subTest(args=args, overrides=overrides):
                environment = {**self.environment, **overrides}
                result = subprocess.run(
                    [str(UV_SHADOW), *args],
                    capture_output=True,
                    check=False,
                    env=environment,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("active=", result.stdout)
                self.assertNotIn("active=1", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
