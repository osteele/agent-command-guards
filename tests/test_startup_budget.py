"""Every wrapper pays its startup cost on every guarded command.

The requirement is latency, not any particular way of achieving it. Today the
wrappers import nothing outside the standard library and run under whatever
`python3` the ambient PATH supplies, with no virtualenv and no dependency
resolution — but that is the current means, and a change that keeps the budget
is fine. This test measures the requirement so the means stays free to change.

What it catches is the regression that matters: a dependency whose import cost
lands on every `git`, `ssh`, `rsync`, and `uv run` an agent issues.

Measurement is relative and interleaved. Interpreter startup is not the
wrapper's fault, so a baseline is subtracted — but the baseline has to be
sampled in the same window as the subject. A baseline taken once and compared
against samples collected later can yield a *negative* overhead when load
arrives in between, which is impossible (loading a module cannot beat loading
nothing) and is proof that the two windows were not comparable. So each
iteration measures both, and the least subject cost is compared against the
least baseline cost from the same window; a negative result is treated as a
broken instrument and skips rather than passing or failing.
"""

from __future__ import annotations

import subprocess
import sys
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Module-load overhead attributable to the wrapper, above interpreter startup.
# Generous: a single third-party import typically costs several times this.
# Measured on a loaded laptop, the wrappers sit between 8ms and 43ms.
BUDGET_MS = 150.0

RUNS = 7

SUBJECTS = {
    "shadow_wrapper.py": REPO / "shadows" / "shadow_wrapper.py",
    "uv": REPO / "shadows" / "uv",
    "agent-model": REPO / "launchers" / "agent-model",
}

# Loads the file as a module without running it. SourceFileLoader is named
# explicitly because three of the four subjects have no `.py` extension —
# filenames are the command interface here — and the suffix-based helpers
# return no loader for them. Every subject guards its entry point behind
# __main__, so import executes definitions only. dataclasses and typing resolve
# annotations through sys.modules, so the module registers itself first.
LOAD_SOURCE = """
import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
loader = SourceFileLoader("subject", sys.argv[1])
module = module_from_spec(spec_from_loader("subject", loader))
sys.modules["subject"] = module
loader.exec_module(module)
"""

BASELINE = [sys.executable, "-c", ""]


def _run_ms(argv: list[str]) -> float:
    started = time.perf_counter()
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    elapsed = (time.perf_counter() - started) * 1000.0
    if result.returncode != 0:
        raise AssertionError(
            f"{' '.join(argv)} exited {result.returncode}: {result.stderr.strip()}"
        )
    return elapsed


def _overhead_ms(argv: list[str]) -> float:
    """Least observed cost of `argv` above interpreter startup.

    Both minima come from one interleaved window, so load that arrives during
    the run inflates both series rather than only one. Comparing minima rather
    than taking the minimum per-iteration difference matters: the minimum of
    noisy differences is biased downward, because it selects the iteration whose
    baseline was slowest and whose subject was fastest.
    """
    baselines: list[float] = []
    subjects: list[float] = []
    for _ in range(RUNS):
        baselines.append(_run_ms(BASELINE))
        subjects.append(_run_ms(argv))
    return min(subjects) - min(baselines)


class StartupBudgetTest(unittest.TestCase):
    def test_every_subject_is_present(self) -> None:
        """A renamed or moved wrapper must not silently drop out of coverage."""
        for name, path in SUBJECTS.items():
            with self.subTest(subject=name):
                self.assertTrue(path.is_file(), f"{path} is missing")

    def test_wrapper_load_stays_within_budget(self) -> None:
        for name, path in SUBJECTS.items():
            with self.subTest(subject=name):
                overhead = _overhead_ms([sys.executable, "-c", LOAD_SOURCE, str(path)])
                if overhead < 0:
                    self.skipTest(
                        f"{name} measured {overhead:.0f}ms, which is impossible; "
                        "the machine is too loaded for this measurement to mean "
                        "anything"
                    )
                self.assertLess(
                    overhead,
                    BUDGET_MS,
                    f"{name} adds {overhead:.0f}ms over interpreter startup, above "
                    f"the {BUDGET_MS:.0f}ms budget. This cost is paid on every "
                    "guarded command in every agent session.",
                )


if __name__ == "__main__":
    unittest.main()
