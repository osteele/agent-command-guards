"""Tests for the uv shadow.

The guard itself is the `with-limits` crate and is tested there; what is left
to pin here is the shadow's own decisions -- which uv invocations get wrapped,
which are left alone, and that the wrapped ones really do come out the far side
with the guard's environment on them.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SHADOWS = Path(__file__).resolve().parent.parent / "shadows"
UV_SHADOW = SHADOWS / "uv"
CARGO_BIN = Path.home() / ".cargo" / "bin"

requires_guard = unittest.skipUnless(
    (CARGO_BIN / "with-limits").exists(),
    "with-limits is not installed (cargo install with-limits)",
)


@requires_guard
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
            "printf 'active=%s\\n' \"${WITH_LIMITS_ACTIVE:-}\"\n"
            "printf 'high=%s\\n' \"${PYTORCH_MPS_HIGH_WATERMARK_RATIO:-}\"\n"
        )
        self.real_uv.chmod(0o755)
        self.environment = dict(os.environ)
        self.environment["PATH"] = (
            f"{SHADOWS}:{self.shim_bin}:{self.real_bin}:{CARGO_BIN}:/usr/bin:/bin"
        )
        for name in (
            "WITH_LIMITS_ACTIVE",
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
