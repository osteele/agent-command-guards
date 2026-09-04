"""Every wrapper pays its startup cost on every guarded command.

The requirement is latency, not any particular way of achieving it. Today the
wrappers import nothing outside the standard library and run under whatever
`python3` the ambient PATH supplies, with no virtualenv and no dependency
resolution — but that is the current means, and a change that keeps the budget
is fine. This test measures the requirement so the means stays free to change.

What it catches is the regression that matters: a dependency whose import cost
lands on every `git`, `ssh`, `rsync`, and `uv run` an agent issues.

Measurement is relative. Interpreter startup is not the wrapper's fault, so the
baseline is subtracted, and the minimum of several runs is used because a loaded
machine inflates individual samples but cannot deflate them. When the baseline
itself is implausibly slow the machine is too busy to measure and the test skips
rather than reporting a fault it cannot substantiate.
"""

from __future__ import annotations

import subprocess
import sys
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Module-load overhead attributable to the wrapper, above interpreter startup.
# Generous: a single third-party import typically costs several times this,
# while the largest wrapper today measures around 26ms.
BUDGET_MS = 150.0
# Above this, the machine is too loaded for the measurement to mean anything.
BASELINE_CEILING_MS = 600.0

RUNS = 5

SUBJECTS = {
    "shadow_wrapper.py": REPO / "shadows" / "shadow_wrapper.py",
    "uv": REPO / "shadows" / "uv",
    "with-limits": REPO / "shadows" / "with-limits",
    "agent-model": REPO / "launchers" / "agent-model",
}

# Loads the file as a module without running it. SourceFileLoader is named
# explicitly because three of the four subjects have no `.py` extension —
# filenames are the command interface here — and the suffix-based helpers
# return no loader for them. Every subject guards its entry point behind
# __main__, so import executes definitions only.
LOAD_SOURCE = """
import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
loader = SourceFileLoader("subject", sys.argv[1])
module = module_from_spec(spec_from_loader("subject", loader))
# dataclasses and typing resolve annotations through sys.modules, so a module
# executed outside the import system has to register itself first.
sys.modules["subject"] = module
loader.exec_module(module)
"""


def _best_ms(argv: list[str]) -> float:
    best = float("inf")
    for _ in range(RUNS):
        started = time.perf_counter()
        result = subprocess.run(argv, capture_output=True, text=True, check=False)
        elapsed = (time.perf_counter() - started) * 1000.0
        if result.returncode != 0:
            raise AssertionError(
                f"{' '.join(argv)} exited {result.returncode}: {result.stderr.strip()}"
            )
        best = min(best, elapsed)
    return best


class StartupBudgetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.baseline = _best_ms([sys.executable, "-c", ""])

    def setUp(self) -> None:
        if self.baseline > BASELINE_CEILING_MS:
            self.skipTest(
                f"interpreter baseline {self.baseline:.0f}ms exceeds "
                f"{BASELINE_CEILING_MS:.0f}ms; machine too loaded to measure"
            )

    def test_every_subject_is_present(self) -> None:
        """A renamed or moved wrapper must not silently drop out of coverage."""
        for name, path in SUBJECTS.items():
            with self.subTest(subject=name):
                self.assertTrue(path.is_file(), f"{path} is missing")

    def test_wrapper_load_stays_within_budget(self) -> None:
        for name, path in SUBJECTS.items():
            with self.subTest(subject=name):
                loaded = _best_ms([sys.executable, "-c", LOAD_SOURCE, str(path)])
                overhead = loaded - self.baseline
                self.assertLess(
                    overhead,
                    BUDGET_MS,
                    f"{name} adds {overhead:.0f}ms over interpreter startup "
                    f"({loaded:.0f}ms vs {self.baseline:.0f}ms baseline), above "
                    f"the {BUDGET_MS:.0f}ms budget. This cost is paid on every "
                    "guarded command in every agent session.",
                )


if __name__ == "__main__":
    unittest.main()
