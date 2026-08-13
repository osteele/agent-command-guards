"""Contract and parser tests for the SSH, SCP, and rsync shadows."""

from __future__ import annotations

import importlib.util
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

HERE = Path(__file__).resolve().parent
WRAPPER = HERE / "shadow_wrapper.py"

spec = importlib.util.spec_from_file_location("shadow_wrapper", WRAPPER)
if spec is None or spec.loader is None:
    raise RuntimeError("could not load shadow_wrapper.py")
shadow_wrapper = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = shadow_wrapper
spec.loader.exec_module(shadow_wrapper)


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

    def test_non_connecting_ssh_modes_do_not_probe(self) -> None:
        for option, args in (
            ("-G", ["-G", "beta"]),
            ("-Q", ["-Q", "cipher", "beta"]),
            ("-V", ["-V", "beta"]),
        ):
            with self.subTest(option=option):
                self.assertEqual(shadow_wrapper.find_target_hosts(args, "ssh"), set())

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


class StateRecoveryTest(unittest.TestCase):
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
        )
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            with mock.patch.object(shadow_wrapper, "STATE_FILE", state_file):
                for document in invalid_documents:
                    with self.subTest(document=document):
                        state_file.write_text(json.dumps(document))
                        self.assertEqual(shadow_wrapper.load_state(), {})


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
