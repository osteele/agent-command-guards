"""Contract and parser tests for the SSH, SCP, and rsync shadows."""

from __future__ import annotations

import importlib.util
import contextlib
import io
import json
import os
import random
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
    WALK_SEEDS = (20260813, 20260814, 20260815, 20260816, 20260817)

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
        """Exercise every (state, event) pair once and verify the outcome."""
        events = ("success", "fail_accept", "fail_decline")
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
                                    return_value=event.endswith("accept"),
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
        """Generate long interleaved event sequences and check the model invariant."""
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
                                        ("success", "fail_accept", "fail_decline")
                                    )

                                    expected_state, expected_return, expected_dialog = (
                                        self._expected_transition(model, host, event)
                                    )

                                    mock_probe.return_value = event == "success"
                                    mock_dialog.return_value = event.endswith("accept")

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

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_shadow(self, command: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.guard_bin / command), *args],
            capture_output=True,
            check=False,
            env=self.environment,
            text=True,
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
    otherwise prints its arguments; the fake ``osascript`` logs each dialog
    and answers from ``DIALOG_ANSWER``.
    """

    def setUp(self) -> None:
        super().setUp()
        ssh = self.real_bin / "ssh"
        ssh.write_text(
            "#!/bin/sh\n"
            "for arg in \"$@\"; do\n"
            '  if [ "$arg" = "BatchMode=yes" ]; then\n'
            '    if [ "${PROBE_OK:-}" = "1" ]; then exit 0; fi\n'
            "    exit 1\n"
            "  fi\n"
            "done\n"
            "printf 'ssh args=%s\\n' \"$*\"\n"
        )
        self.dialog_log = self.tmp / "dialog.log"
        osascript = self.real_bin / "osascript"
        osascript.write_text(
            "#!/bin/sh\n"
            f"printf 'dialog\\n' >> \"{self.dialog_log}\"\n"
            'if [ "${DIALOG_ANSWER:-yes}" = "no" ]; then\n'
            "  printf 'button returned:No\\n'\n"
            "  exit 0\n"
            "fi\n"
            "printf 'button returned:Yes\\n'\n"
            "exit 0\n"
        )
        osascript.chmod(0o755)
        self.environment.pop("PROBE_OK", None)
        self.environment["DIALOG_ANSWER"] = "yes"

    def state_document(self) -> dict:
        state_file = self.tmp / ".cache" / "agent-command-guards" / "state.json"
        return json.loads(state_file.read_text())

    def dialogs_shown(self) -> int:
        if not self.dialog_log.exists():
            return 0
        return len(self.dialog_log.read_text().splitlines())

    def test_unreachable_host_with_yes_proceeds_and_records(self) -> None:
        result = self.run_shadow("ssh", "beta", "true")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "ssh args=beta true\n")
        self.assertEqual(self.dialogs_shown(), 1)
        beta = self.state_document()["beta"]
        self.assertFalse(beta["declined"])
        self.assertFalse(beta["was_accessible"])
        self.assertTrue(beta["last_checked"])

    def test_unreachable_host_with_no_aborts_and_is_remembered(self) -> None:
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

    def test_concurrent_dialogs_serialize_state_writes(self) -> None:
        # Several agents hit the same unreachable host at once; the flock and
        # atomic rename must leave one valid document, not interleaved JSON.
        processes = [
            subprocess.Popen(
                [str(self.guard_bin / "ssh"), "beta", "true"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.environment,
                text=True,
            )
            for _ in range(6)
        ]
        outcomes = [process.communicate() for process in processes]

        for process, (stdout, _) in zip(processes, outcomes, strict=True):
            self.assertEqual(process.returncode, 0, stdout)
            self.assertEqual(stdout, "ssh args=beta true\n")
        self.assertEqual(self.dialogs_shown(), 6)
        document = self.state_document()
        self.assertEqual(
            sorted(document),
            ["beta"],
        )
        self.assertFalse(document["beta"]["declined"])
        state_dir = self.tmp / ".cache" / "agent-command-guards"
        self.assertEqual(
            sorted(path.name for path in state_dir.iterdir()),
            ["state.json", "state.lock"],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
