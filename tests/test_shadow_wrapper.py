"""Contract and parser tests for the SSH, SCP, and rsync shadows."""

from __future__ import annotations

import importlib.util
import contextlib
import io
import json
import os
import random
import select
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

SHADOWS = Path(__file__).resolve().parent.parent / "shadows"
WRAPPER = SHADOWS / "shadow_wrapper.py"

spec = importlib.util.spec_from_file_location("shadow_wrapper", WRAPPER)
if spec is None or spec.loader is None:
    raise RuntimeError("could not load shadow_wrapper.py")
shadow_wrapper = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = shadow_wrapper
spec.loader.exec_module(shadow_wrapper)

requires_posix_shell = unittest.skipIf(
    os.name == "nt",
    "executable shell-script fakes and which(1) need a POSIX system",
)


class ManagedHostParsingTest(unittest.TestCase):
    def test_generated_managed_host_variants_are_guarded(self) -> None:
        seed = 20260813
        generator = random.Random(seed)
        features: Counter[str] = Counter()

        for step in range(240):
            command = generator.choice(("ssh", "scp", "rsync"))
            canonical_host = generator.choice(sorted(shadow_wrapper.TARGET_HOSTS))
            host = "".join(
                character.upper() if generator.random() < 0.5 else character
                for character in canonical_host
            )
            if generator.choice((False, True)):
                host += "."
                features["trailing-dot"] += 1
            user = "agent@" if generator.choice((False, True)) else ""
            if user:
                features["user"] += 1

            if command == "ssh":
                args = [f"{user}{host}"]
            else:
                use_uri = generator.choice((False, True))
                if use_uri:
                    scheme = "scp" if command == "scp" else "rsync"
                    endpoint = f"{scheme}://{user}{host}/destination"
                    features["uri"] += 1
                else:
                    endpoint = f"{user}{host}:destination"
                    features["colon"] += 1
                args = ["source", endpoint]
            features[command] += 1

            with self.subTest(seed=seed, step=step, command=command, args=args):
                self.assertEqual(
                    shadow_wrapper.find_target_hosts(args, command),
                    {canonical_host},
                )

        for feature in (
            "ssh",
            "scp",
            "rsync",
            "uri",
            "colon",
            "user",
            "trailing-dot",
        ):
            with self.subTest(feature=feature):
                self.assertGreater(features[feature], 0)

    def test_dns_equivalent_managed_host_spellings_are_guarded(self) -> None:
        for destination in ("beta", "BETA", "beta.", "user@Beta."):
            with self.subTest(destination=destination):
                self.assertEqual(
                    shadow_wrapper.find_target_hosts([destination], "ssh"),
                    {"beta"},
                )

    def test_host_normalization_boundaries(self) -> None:
        for value, expected in (
            ("user@host", "host"),
            ("[2001:db8::1]:2222", "2001:db8::1"),
            ("[beta", "[beta"),
            ("[host]suffix]", "host"),
            ("2001:db8::1", "2001:db8::1"),
            ("host:2222", "host"),
            ("example.x", "example.x"),
            ("EXAMPLE.X", "example.x"),
        ):
            with self.subTest(value=value):
                self.assertEqual(shadow_wrapper.normalize_host(value), expected)

    def test_ssh_option_values_are_not_hosts(self) -> None:
        for args in (
            ["-p", "22", "beta"],
            ["-p22", "beta"],
            ["-o", "ProxyCommand=none", "beta"],
        ):
            with self.subTest(args=args):
                self.assertEqual(
                    shadow_wrapper.find_target_hosts(args, "ssh"), {"beta"}
                )

    def test_ssh_option_terminator_is_respected(self) -> None:
        self.assertEqual(shadow_wrapper.extract_ssh_host(["--", "-V"]), "-v")
        self.assertFalse(shadow_wrapper.is_non_connecting_ssh_invocation(["--", "-V"]))

    def test_non_connecting_ssh_modes_do_not_probe(self) -> None:
        for option, args in (
            ("-G", ["-G", "beta"]),
            ("-Q", ["-Q", "cipher", "beta"]),
            ("-V", ["-V", "beta"]),
        ):
            with self.subTest(option=option):
                self.assertEqual(shadow_wrapper.find_target_hosts(args, "ssh"), set())

    def test_non_connecting_ssh_modes_follow_other_options(self) -> None:
        for args in (
            ["-p", "22", "-V"],
            ["-x", "-V"],
        ):
            with self.subTest(args=args):
                self.assertTrue(shadow_wrapper.is_non_connecting_ssh_invocation(args))

    def test_remote_command_options_do_not_bypass_ssh_guard(self) -> None:
        for args in (
            ["beta", "echo", "-V"],
            ["beta", "command", "-G", "value"],
        ):
            with self.subTest(args=args):
                self.assertEqual(
                    shadow_wrapper.find_target_hosts(args, "ssh"), {"beta"}
                )

    def test_scp_uri_is_guarded(self) -> None:
        for destination in (
            "scp://beta/path",
            "scp://user@BETA:2222/path",
        ):
            with self.subTest(destination=destination):
                self.assertEqual(
                    shadow_wrapper.find_target_hosts(["source", destination], "scp"),
                    {"beta"},
                )

    def test_scp_remote_source_and_option_values_are_distinguished(self) -> None:
        self.assertEqual(
            shadow_wrapper.find_target_hosts(["beta:source", "destination"], "scp"),
            {"beta"},
        )
        self.assertEqual(
            shadow_wrapper.find_target_hosts(
                ["-o", "beta:path", "source", "destination"], "scp"
            ),
            set(),
        )
        self.assertEqual(
            shadow_wrapper.find_target_hosts(
                ["-o", "ProxyCommand=none", "source", "beta:destination"],
                "scp",
            ),
            {"beta"},
        )

    def test_unmanaged_hosts_are_not_guarded(self) -> None:
        for command, args in (
            ("ssh", ["example.com"]),
            ("scp", ["source", "scp://example.com/path"]),
            ("rsync", ["source/", "example.com:destination/"]),
        ):
            with self.subTest(command=command, args=args):
                self.assertEqual(shadow_wrapper.find_target_hosts(args, command), set())

    def test_rsync_filter_argument_is_not_treated_as_an_endpoint(self) -> None:
        for args in (
            ["--exclude", "beta:cache", "source/", "destination/"],
            ["--exclude=beta:cache", "source/", "destination/"],
            ["-f", "- beta:cache", "source/", "destination/"],
        ):
            with self.subTest(args=args):
                self.assertEqual(shadow_wrapper.find_target_hosts(args, "rsync"), set())

    def test_rsync_remote_endpoint_after_options_is_guarded(self) -> None:
        args = ["--exclude", "cache", "source/", "user@BETA.:destination/"]
        self.assertEqual(shadow_wrapper.find_target_hosts(args, "rsync"), {"beta"})

    def test_rsync_daemon_endpoint_is_guarded(self) -> None:
        self.assertEqual(
            shadow_wrapper.find_target_hosts(["source/", "BETA::module"], "rsync"),
            {"beta"},
        )

    def test_rsync_flag_does_not_consume_endpoint(self) -> None:
        for args in (
            ["--archive", "source/", "beta:destination/"],
            ["--archive", "beta:destination/"],
        ):
            with self.subTest(args=args):
                self.assertEqual(
                    shadow_wrapper.find_target_hosts(args, "rsync"), {"beta"}
                )

    def test_rsync_option_terminator_preserves_option_like_operands(self) -> None:
        self.assertEqual(
            list(
                shadow_wrapper.iterate_non_option_args_rsync(
                    ["--", "--exclude", "beta:destination/"]
                )
            ),
            ["--exclude", "beta:destination/"],
        )


class StateRecoveryTest(unittest.TestCase):
    def test_valid_state_is_restored(self) -> None:
        document = {
            "beta": {
                "declined": True,
                "last_checked": "2026-08-13T19:00:00+00:00",
                "was_accessible": False,
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            state_file.write_text(json.dumps(document))
            with mock.patch.object(shadow_wrapper, "STATE_FILE", state_file):
                self.assertEqual(
                    shadow_wrapper.load_state(),
                    {
                        "beta": shadow_wrapper.HostState(
                            declined=True,
                            last_checked="2026-08-13T19:00:00+00:00",
                            was_accessible=False,
                        )
                    },
                )

    def test_structurally_invalid_json_is_treated_as_empty_state(self) -> None:
        invalid_documents = (
            [],
            "state",
            7,
            {"beta": []},
            {
                "beta": {
                    "declined": "false",
                    "last_checked": "today",
                    "was_accessible": False,
                }
            },
            {
                "beta": {
                    "declined": False,
                    "last_checked": 0,
                    "was_accessible": False,
                }
            },
            {
                "beta": {
                    "declined": False,
                    "last_checked": "today",
                    "was_accessible": 0,
                }
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            with mock.patch.object(shadow_wrapper, "STATE_FILE", state_file):
                for document in invalid_documents:
                    with self.subTest(document=document):
                        state_file.write_text(json.dumps(document))
                        self.assertEqual(shadow_wrapper.load_state(), {})

    def test_failed_save_leaves_previous_decisions_intact(self) -> None:
        # A crash or full disk mid-save must not replace the recorded
        # decisions with a truncated document, and must not litter the cache
        # directory with temporary files.
        previous = {
            "beta": shadow_wrapper.HostState(
                declined=True,
                last_checked="2026-08-13T19:00:00+00:00",
                was_accessible=False,
            )
        }
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            with (
                mock.patch.object(shadow_wrapper, "STATE_DIR", Path(directory)),
                mock.patch.object(shadow_wrapper, "STATE_FILE", state_file),
            ):
                shadow_wrapper.save_state(previous)
                with mock.patch.object(
                    shadow_wrapper.os, "replace", side_effect=OSError("disk full")
                ):
                    with self.assertRaises(OSError):
                        shadow_wrapper.save_state(
                            {
                                "alpha": shadow_wrapper.HostState(
                                    declined=False,
                                    last_checked="2026-08-18T19:00:00+00:00",
                                    was_accessible=True,
                                )
                            }
                        )
                self.assertEqual(shadow_wrapper.load_state(), previous)
            self.assertEqual(
                sorted(path.name for path in Path(directory).iterdir()),
                ["state.json"],
            )


class HostDialogStateMachineTest(unittest.TestCase):
    """Model-based tests for the per-host dialog/decision state machine.

    The wrapper keeps a small amount of persistent state per managed host so it
    can avoid re-prompting after the user declines the network-change dialog.
    These tests verify that the real state transitions match a simple model for
    generated sequences of probe outcomes and user responses.
    """

    HOSTS = sorted(shadow_wrapper.TARGET_HOSTS)
    DIALOG_RESULTS = {"success": None, "fail_accept": True, "fail_decline": False, "fail_unavailable": None}
    WALK_SEEDS = (20260813, 20260814, 20260815, 20260816, 20260817)

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        for name, value in (("STATE_DIR", root), ("LOCK_FILE", root / "state.lock")):
            patcher = mock.patch.object(shadow_wrapper, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _model_to_fields(self, model_state: str) -> tuple[bool, bool]:
        """Return (declined, was_accessible) for a model state name."""
        return {
            "UNKNOWN": (False, False),
            "ACCESSIBLE": (False, True),
            "INACCESSIBLE": (False, False),
            "DECLINED": (True, False),
        }[model_state]

    def _expected_transition(
        self, model: dict[str, str], host: str, event: str
    ) -> tuple[str, bool, bool]:
        """Given a model state and event, return (new_state, return_value, dialog_shown)."""
        state = model.get(host, "UNKNOWN")
        if event == "success":
            return "ACCESSIBLE", True, False
        if state == "DECLINED":
            return state, False, False
        if event == "fail_accept":
            return "INACCESSIBLE", True, True
        if event == "fail_unavailable":
            return "INACCESSIBLE", False, True
        # event == "fail_decline"
        return "DECLINED", False, True

    def _assert_state_matches_model(
        self, model: dict[str, str], actual: dict[str, shadow_wrapper.HostState]
    ) -> None:
        """Compare the on-disk state to the model, ignoring timestamps."""
        self.assertEqual(set(actual.keys()), set(model.keys()))
        for host, model_state in model.items():
            declined, was_accessible = self._model_to_fields(model_state)
            host_state = actual[host]
            self.assertEqual(host_state.declined, declined, f"{host} declined mismatch")
            self.assertEqual(
                host_state.was_accessible, was_accessible, f"{host} was_accessible mismatch"
            )

    def test_all_single_step_transitions_match_model(self) -> None:
        """HandleProbeResult and all three confirmation outcome rules."""
        events = tuple(self.DIALOG_RESULTS)
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            for initial_state in ("UNKNOWN", "ACCESSIBLE", "INACCESSIBLE", "DECLINED"):
                for event in events:
                    with self.subTest(initial_state=initial_state, event=event):
                        state_file.write_text("{}")
                        model: dict[str, str] = {}
                        with mock.patch.object(shadow_wrapper, "STATE_FILE", state_file):
                            if initial_state != "UNKNOWN":
                                declined, was_accessible = self._model_to_fields(initial_state)
                                seed_state = {
                                    "beta": shadow_wrapper.HostState(
                                        declined=declined,
                                        last_checked="2026-08-13T19:00:00+00:00",
                                        was_accessible=was_accessible,
                                    )
                                }
                                shadow_wrapper.save_state(seed_state)
                                model["beta"] = initial_state

                            expected_state, expected_return, expected_dialog = (
                                self._expected_transition(model, "beta", event)
                            )

                            with mock.patch.object(
                                shadow_wrapper, "probe_host", return_value=(event == "success")
                            ) as mock_probe:
                                with mock.patch.object(
                                    shadow_wrapper,
                                    "show_network_dialog",
                                    return_value=self.DIALOG_RESULTS[event],
                                ) as mock_dialog:
                                    with contextlib.redirect_stderr(io.StringIO()):
                                        result = shadow_wrapper.check_host("beta", "/usr/bin/ssh")

                            self.assertEqual(result, expected_return)
                            self.assertEqual(mock_probe.call_count, 1)
                            if expected_dialog:
                                mock_dialog.assert_called_once_with("beta")
                            else:
                                mock_dialog.assert_not_called()

                            if event == "success":
                                model["beta"] = expected_state
                            elif initial_state != "DECLINED":
                                model["beta"] = expected_state

                            self._assert_state_matches_model(
                                model, shadow_wrapper.load_state()
                            )

    def test_random_walks_preserve_invariants(self) -> None:
        """SharedDecisionPerHost and HandleProbeResult across interleaved hosts."""
        for seed in self.WALK_SEEDS:
            with self.subTest(seed=seed):
                rng = random.Random(seed)
                model: dict[str, str] = {}
                with tempfile.TemporaryDirectory() as directory:
                    state_file = Path(directory) / "state.json"
                    with mock.patch.object(shadow_wrapper, "STATE_FILE", state_file):
                        with mock.patch.object(shadow_wrapper, "probe_host") as mock_probe:
                            with mock.patch.object(
                                shadow_wrapper, "show_network_dialog"
                            ) as mock_dialog:
                                for step in range(rng.randint(50, 150)):
                                    host = rng.choice(self.HOSTS)
                                    event = rng.choice(
                                        tuple(self.DIALOG_RESULTS)
                                    )

                                    expected_state, expected_return, expected_dialog = (
                                        self._expected_transition(model, host, event)
                                    )

                                    mock_probe.return_value = event == "success"
                                    mock_dialog.return_value = self.DIALOG_RESULTS[event]

                                    with contextlib.redirect_stderr(io.StringIO()):
                                        result = shadow_wrapper.check_host(host, "/usr/bin/ssh")

                                    self.assertEqual(
                                        result,
                                        expected_return,
                                        f"seed={seed} step={step} host={host} event={event}",
                                    )
                                    self.assertEqual(
                                        mock_probe.call_count,
                                        1,
                                        f"seed={seed} step={step}",
                                    )
                                    if expected_dialog:
                                        mock_dialog.assert_called_once_with(host)
                                    else:
                                        mock_dialog.assert_not_called()
                                    mock_dialog.reset_mock()
                                    mock_probe.reset_mock()

                                    if event == "success" or (
                                        event != "success" and model.get(host, "UNKNOWN") != "DECLINED"
                                    ):
                                        model[host] = expected_state

                                    self._assert_state_matches_model(
                                        model, shadow_wrapper.load_state()
                                    )


@requires_posix_shell
class ShadowIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.guard_bin = self.tmp / "guards"
        self.guard_bin.mkdir()
        self.real_bin = self.tmp / "real"
        self.real_bin.mkdir()
        for command in ("ssh", "scp", "rsync"):
            (self.guard_bin / command).symlink_to(WRAPPER)
            binary = self.real_bin / command
            binary.write_text(f"#!/bin/sh\nprintf '{command} args=%s\\n' \"$*\"\n")
            binary.chmod(0o755)
        self.environment = dict(os.environ)
        self.environment["HOME"] = str(self.tmp)
        self.environment["PATH"] = f"{self.guard_bin}:{self.real_bin}:/usr/bin:/bin"

    def run_shadow(self, command: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.guard_bin / command), *args],
            capture_output=True,
            check=False,
            env=self.environment,
            text=True,
            timeout=20,
        )

    def test_accessible_managed_ssh_host_delegates_original_arguments(self) -> None:
        result = self.run_shadow("ssh", "BETA", "echo", "hello")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "ssh args=BETA echo hello\n")

    def test_accessible_scp_uri_delegates_original_arguments(self) -> None:
        destination = "scp://user@beta/path"
        result = self.run_shadow("scp", "source", destination)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"scp args=source {destination}\n")


@requires_posix_shell
class NetworkDialogIntegrationTest(ShadowIntegrationTest):
    """Dialog flows through the real entry point, with fake probe and UI.

    The fake ``ssh`` fails connectivity probes (they carry BatchMode=yes) and
    otherwise prints its arguments. The fake ``osascript`` can wait on an external
    socket barrier so requests demonstrably join before the answer is released.
    """

    def setUp(self) -> None:
        super().setUp()
        ssh = self.real_bin / "ssh"
        ssh.write_text(
            "#!/bin/sh\n"
            "for arg in \"$@\"; do\n"
            '  if [ "$arg" = "BatchMode=yes" ]; then\n'
            f"    printf 'probe\\n' >> {str(self.tmp / 'probe.log')!r}\n"
            '    if [ "${PROBE_OK:-}" = "1" ]; then exit 0; fi\n'
            "    exit 1\n"
            "  fi\n"
            "done\n"
            "printf 'ssh args=%s\\n' \"$*\"\n"
            'exit "${NATIVE_EXIT:-0}"\n'
        )
        self.dialog_log = self.tmp / "dialog.log"
        osascript = self.real_bin / "osascript"
        osascript.write_text(
            f"#!{sys.executable}\n"
            "import json, os, re, socket, sys\n"
            f"with open({str(self.dialog_log)!r}, 'a') as log:\n"
            "    log.write('dialog\\n')\n"
            "answer = os.environ.get('DIALOG_ANSWER', 'yes')\n"
            "if os.environ.get('DIALOG_CONTROL'):\n"
            "    with socket.create_connection(('127.0.0.1', int(os.environ['DIALOG_CONTROL'])), 20) as connection:\n"
            "        host = re.search(r'Cannot reach (\\w+)', sys.argv[-1]).group(1)\n"
            "        connection.sendall((json.dumps({'host': host, 'pid': os.getpid()}) + '\\n').encode())\n"
            "        answer = connection.recv(128).decode().strip()\n"
            "if answer == 'no':\n"
            "    print('button returned:No')\n"
            "elif answer == 'yes':\n"
            "    print('button returned:Yes')\n"
            "elif answer == 'malformed':\n"
            "    print('unrecognized response mentioning Yes')\n"
            "else:\n"
            "    print('button returned:No')\n"
            "    sys.exit(1)\n"
        )
        osascript.chmod(0o755)
        self.environment.pop("PROBE_OK", None)
        self.environment["DIALOG_ANSWER"] = "yes"
        self.environment.pop("NATIVE_EXIT", None)
        self.environment.pop("DIALOG_CONTROL", None)
        self.processes: list[subprocess.Popen[str]] = []
        self.controls: list[socket.socket] = []
        self.server: socket.socket | None = None
        self.addCleanup(self.cleanup_processes)

    def cleanup_processes(self) -> None:
        for connection in self.controls:
            connection.close()
        if self.server is not None:
            self.server.close()
        for process in self.processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=20)

    def control_dialogs(self) -> None:
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen()
        self.server.settimeout(20)
        self.environment["DIALOG_CONTROL"] = str(self.server.getsockname()[1])

    def take_dialog(self, host: str) -> socket.socket:
        assert self.server is not None
        connection, _ = self.server.accept()
        connection.settimeout(20)
        self.controls.append(connection)
        payload = bytearray()
        while not payload.endswith(b"\n"):
            chunk = connection.recv(1024)
            if not chunk:
                self.fail("dialog closed before reporting its host")
            payload.extend(chunk)
        self.assertEqual(json.loads(payload)["host"], host)
        return connection

    def start_request(
        self, host: str = "beta", environment: dict[str, str] | None = None
    ) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            [str(self.guard_bin / "ssh"), host, "true"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.environment if environment is None else environment,
            text=True,
            start_new_session=True,
        )
        self.processes.append(process)
        return process

    def wait_for_join(self, process: subprocess.Popen[str]) -> None:
        # The wrapper reports its wait only after atomically joining. This is a
        # synchronization barrier, not an assertion about diagnostic wording.
        assert process.stderr is not None
        ready, _, _ = select.select([process.stderr], [], [], 20)
        if not ready or not process.stderr.readline():
            self.fail("request did not reach the shared-confirmation wait")

    def joined_group(self) -> tuple[list[subprocess.Popen[str]], socket.socket]:
        self.control_dialogs()
        processes = [self.start_request()]
        dialog = self.take_dialog("beta")
        for _ in range(3):
            process = self.start_request()
            processes.append(process)
            self.wait_for_join(process)
        return processes, dialog

    def assert_outcome(self, process: subprocess.Popen[str], accepted: bool, host: str = "beta") -> None:
        stdout, stderr = process.communicate(timeout=20)
        self.assertEqual(process.returncode, 0 if accepted else 1, stderr)
        self.assertEqual(stdout, f"ssh args={host} true\n" if accepted else "")

    def state_document(self) -> dict:
        state_file = self.tmp / ".cache" / "agent-command-guards" / "state.json"
        return json.loads(state_file.read_text())

    def dialogs_shown(self) -> int:
        if not self.dialog_log.exists():
            return 0
        return len(self.dialog_log.read_text().splitlines())

    def test_unreachable_host_with_yes_proceeds_and_records(self) -> None:
        """AcceptConnectionConfirmation authorizes the requested native command."""
        result = self.run_shadow("ssh", "beta", "true")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "ssh args=beta true\n")
        self.assertEqual(self.dialogs_shown(), 1)
        beta = self.state_document()["beta"]
        self.assertFalse(beta["declined"])
        self.assertFalse(beta["was_accessible"])

    def test_unreachable_host_with_no_aborts_and_is_remembered(self) -> None:
        """RememberConnectionRefusal persists across independent caller processes."""
        self.environment["DIALOG_ANSWER"] = "no"

        result = self.run_shadow("ssh", "beta", "true")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("ssh args=", result.stdout)
        self.assertIn("not accessible", result.stderr)
        self.assertEqual(self.dialogs_shown(), 1)
        self.assertTrue(self.state_document()["beta"]["declined"])

        # The remembered decline fails fast: no second dialog, no real ssh.
        result = self.run_shadow("ssh", "beta", "true")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.dialogs_shown(), 1)

    def test_successful_probe_skips_dialog_and_clears_decline(self) -> None:
        """HandleProbeResult clears shared refusal on a subsequent successful probe."""
        self.environment["DIALOG_ANSWER"] = "no"
        self.assertEqual(self.run_shadow("ssh", "beta", "true").returncode, 1)
        self.assertTrue(self.state_document()["beta"]["declined"])

        self.environment["PROBE_OK"] = "1"
        result = self.run_shadow("ssh", "beta", "true")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "ssh args=beta true\n")
        self.assertEqual(self.dialogs_shown(), 1)
        beta = self.state_document()["beta"]
        self.assertFalse(beta["declined"])
        self.assertTrue(beta["was_accessible"])

    def test_joined_requests_share_yes_without_standing_approval(self) -> None:
        """OnePendingConfirmationPerHost, SharedConfirmationAnswer, SharedHostConfirmation."""
        processes, dialog = self.joined_group()
        dialog.sendall(b"yes\n")
        for process in processes:
            self.assert_outcome(process, True)
        self.assertEqual(self.dialogs_shown(), 1)

        later = self.start_request()
        self.take_dialog("beta").sendall(b"no\n")
        self.assert_outcome(later, False)
        self.assertEqual(self.dialogs_shown(), 2)

    def test_later_group_cannot_replace_answer_for_suspended_joiner(self) -> None:
        """SharedConfirmationAnswer retains the first group's answer for slow consumers."""
        import fcntl

        processes, dialog = self.joined_group()
        delayed = processes[-1]
        state_dir = self.tmp / ".cache" / "agent-command-guards"
        # Stop the consumer only while it cannot own the shared state lock.
        # Otherwise SIGSTOP can freeze the coordinator rather than its consumer.
        with (state_dir / "state.lock").open("r+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            delayed.send_signal(signal.SIGSTOP)
            _, status = os.waitpid(delayed.pid, os.WUNTRACED)
            self.assertTrue(os.WIFSTOPPED(status))
        dialog.sendall(b"yes\n")
        for process in processes[:-1]:
            self.assert_outcome(process, True)
        later = self.start_request()
        self.take_dialog("beta").sendall(b"no\n")
        self.assert_outcome(later, False)
        delayed.send_signal(signal.SIGCONT)
        self.assert_outcome(delayed, True)
        self.assertEqual(self.dialogs_shown(), 2)

    def test_joined_requests_share_no_and_later_requests_remember_it(self) -> None:
        """RememberConnectionRefusal and SharedDecisionPerHost apply across sessions."""
        processes, dialog = self.joined_group()
        dialog.sendall(b"no\n")
        for process in processes:
            self.assert_outcome(process, False)
        self.assert_outcome(self.start_request(), False)
        self.assertEqual(self.dialogs_shown(), 1)
        self.assertTrue(self.state_document()["beta"]["declined"])

    def test_joined_requests_share_unavailable_without_remembering_decline(self) -> None:
        """FailWhenConfirmationUnavailable is not RememberConnectionRefusal."""
        processes, dialog = self.joined_group()
        dialog.sendall(b"unavailable\n")
        for process in processes:
            self.assert_outcome(process, False)
        self.assertEqual(self.dialogs_shown(), 1)
        self.assertFalse(self.state_document()["beta"]["declined"])

        later = self.start_request()
        self.take_dialog("beta").sendall(b"yes\n")
        self.assert_outcome(later, True)
        self.assertEqual(self.dialogs_shown(), 2)

    def test_joiner_does_not_need_its_own_confirmation_surface(self) -> None:
        """SharedHostConfirmation allows a dialog-less request to join a viable prompt."""
        self.control_dialogs()
        owner = self.start_request()
        dialog = self.take_dialog("beta")
        unavailable_bin = self.tmp / "unavailable-ui"
        unavailable_bin.mkdir()
        missing_ui = unavailable_bin / "osascript"
        missing_ui.write_text("#!/bin/sh\nexit 127\n")
        missing_ui.chmod(0o755)
        environment = dict(self.environment)
        environment["PATH"] = f"{unavailable_bin}:{self.environment['PATH']}"
        joiner = self.start_request(environment=environment)
        self.wait_for_join(joiner)
        dialog.sendall(b"yes\n")
        self.assert_outcome(owner, True)
        self.assert_outcome(joiner, True)
        self.assertEqual(self.dialogs_shown(), 1)

    def test_different_hosts_prompt_independently(self) -> None:
        """DecisionMatchesRequestedHost and ConfirmationMatchesAttemptHost isolate hosts."""
        self.control_dialogs()
        beta = self.start_request("beta")
        beta_dialog = self.take_dialog("beta")
        alpha = self.start_request("alpha")
        alpha_dialog = self.take_dialog("alpha")
        alpha_dialog.sendall(b"yes\n")
        self.assert_outcome(alpha, True, "alpha")
        beta_dialog.sendall(b"no\n")
        self.assert_outcome(beta, False)
        self.assertFalse(self.state_document()["alpha"]["declined"])
        self.assertTrue(self.state_document()["beta"]["declined"])

    def test_owner_death_releases_joiners_without_persistent_decline(self) -> None:
        """FailWhenConfirmationUnavailable bounds a dead owner's pending confirmation."""
        processes, dialog = self.joined_group()
        owner, *joiners = processes
        owner.kill()
        owner.wait(timeout=20)
        dialog.sendall(b"unavailable\n")
        for process in joiners:
            self.assert_outcome(process, False)
        self.assertFalse(self.state_document()["beta"]["declined"])
        later = self.start_request()
        self.take_dialog("beta").sendall(b"yes\n")
        self.assert_outcome(later, True)
        self.assertEqual(self.dialogs_shown(), 2)

    def test_expired_confirmation_refuses_every_joiner_and_late_owner_answer(self) -> None:
        """FailWhenConfirmationUnavailable is terminal even if a stalled UI later says Yes."""
        import fcntl

        processes, dialog = self.joined_group()
        state_dir = self.tmp / ".cache" / "agent-command-guards"
        # Drive the persisted deadline under the same external file-lock boundary;
        # do not wait for the production five-minute timeout or mock the wrapper.
        with (state_dir / "state.lock").open("r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            token = self.state_document()["beta"]["pending_confirmation"]
            path = state_dir / f"confirmation-beta-{token}.json"
            with path.open("r+") as confirmation_file:
                confirmation = json.load(confirmation_file)
                confirmation["deadline"] = 1
                confirmation_file.seek(0)
                json.dump(confirmation, confirmation_file)
                confirmation_file.truncate()
            fcntl.flock(lock, fcntl.LOCK_UN)
        for process in processes[1:]:
            self.assert_outcome(process, False)
        dialog.sendall(b"yes\n")
        self.assert_outcome(processes[0], False)
        self.assertFalse(self.state_document()["beta"]["declined"])

    def test_unmanaged_host_never_probes_or_prompts_and_keeps_native_exit(self) -> None:
        """PassThroughUnmanagedConnection and ManagedHostAdmission preserve native execution."""
        self.environment["NATIVE_EXIT"] = "23"
        result = self.run_shadow("ssh", "unmanaged.example", "echo", "untouched")
        self.assertEqual(result.returncode, 23)
        self.assertEqual(result.stdout, "ssh args=unmanaged.example echo untouched\n")
        self.assertEqual(self.dialogs_shown(), 0)
        self.assertFalse((self.tmp / "probe.log").exists())

    def test_approval_preserves_native_failure_exit(self) -> None:
        """AuthorizeManagedConnection authorizes execution rather than replacing its result."""
        self.environment["NATIVE_EXIT"] = "23"
        result = self.run_shadow("ssh", "beta", "true")
        self.assertEqual(result.returncode, 23)
        self.assertEqual(result.stdout, "ssh args=beta true\n")
        self.assertEqual(self.dialogs_shown(), 1)

    def test_malformed_dialog_response_is_unavailable_not_approval(self) -> None:
        """FailWhenConfirmationUnavailable requires an explicit native Yes or No answer."""
        self.environment["DIALOG_ANSWER"] = "malformed"
        self.assertEqual(self.run_shadow("ssh", "beta", "true").returncode, 1)
        self.assertFalse(self.state_document()["beta"]["declined"])
        self.environment["DIALOG_ANSWER"] = "yes"
        self.assertEqual(self.run_shadow("ssh", "beta", "true").returncode, 0)
        self.assertEqual(self.dialogs_shown(), 2)

    def test_recovered_host_prompts_again_when_it_becomes_unreachable(self) -> None:
        """HandleProbeResult preserves the accessible-to-unreachable re-prompt transition."""
        self.environment["DIALOG_ANSWER"] = "no"
        self.assertEqual(self.run_shadow("ssh", "beta", "true").returncode, 1)
        self.environment["PROBE_OK"] = "1"
        self.assertEqual(self.run_shadow("ssh", "beta", "true").returncode, 0)
        self.environment.pop("PROBE_OK")
        self.environment["DIALOG_ANSWER"] = "yes"
        self.assertEqual(self.run_shadow("ssh", "beta", "true").returncode, 0)
        self.assertEqual(self.dialogs_shown(), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
