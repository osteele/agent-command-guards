# Decision log

Y-statement entries for decisions that foreclosed something without earning a
full record. Append-only, newest last. An entry is never edited to reflect a
later change; a later change is a later entry.

*In the context of X, facing Y, we decided for Z, and neglected W, to achieve Q,
accepting D.*

- **2026-09-04** — In the context of locating the real binary behind a shadow,
  facing a `uv` shadow that must not resolve to a mise shim, we decided to walk
  `PATH` by hand in that one wrapper while the others use `which -a` with a
  self-path check, and neglected unifying the three lookups into one helper,
  because a mise shim ahead of the concrete binary recurses or hangs once this
  shadow is earlier on `PATH`; accepting duplicated resolution logic that reads
  like an oversight and invites exactly the unification that would reintroduce
  the hang.

- **2026-09-04** — In the context of handing off to the real binary, facing the
  need to preserve exit codes and signal semantics, we decided that every
  wrapper `exec`s its target except `with-limits`, and neglected making the
  guard exec too, because it has to outlive the call to watch the process tree
  it launched; accepting one resident supervisor process inside every guarded
  `uv run` tree.

- **2026-09-04** — In the context of the memory guard's two monitors, facing a
  sandbox that may block process inspection, we decided to enforce the
  available-memory floor only as a fallback when process-tree RSS is
  unavailable, and neglected enforcing both together as defense in depth, so
  that unrelated growth elsewhere on the host cannot terminate an owned tree
  that is below its own ceiling; accepting a deliberate absence that reads like
  a missing safety check and would be "fixed" by a contributor acting in good
  faith. Promote to a full record if that fix is ever attempted.

- **2026-09-04** — In the context of what may live in `shadows/`, facing a
  repository root that used to be the `PATH` entry and therefore turned any
  added file — a `setup`, a `check` — into a command in every agent session, we
  decided that the directory holds exactly what should become a command name and
  nothing else, and neglected keeping the root on `PATH` with an ignore list,
  to make the rule structural rather than remembered; accepting an extra
  directory layer that consumers compute paths through, including
  `agent-tool-policy`, which resolves `with-limits` by relative path and breaks
  if this directory moves. The convention prefers an enforceable invariant here:
  a test asserting every entry in `shadows/` and `launchers/` is an intended
  command name would protect this better than prose.

- **2026-09-04** — In the context of wrapper startup, facing a cost paid on
  every guarded `git`, `ssh`, `rsync`, and `uv run` an agent issues, we decided
  to hold a per-invocation latency budget enforced by
  `tests/test_startup_budget.py`, and neglected recording "standard library
  only" as the rule, because that is one means to the budget rather than the
  requirement and a different implementation that keeps the budget is welcome;
  accepting that the test measures overhead relative to interpreter startup and
  skips when the machine is too loaded to measure, so it bounds regressions
  rather than proving an absolute latency. How the wrappers meet it today —
  no third-party imports, no virtualenv, no dependency resolution — is in
  `CLAUDE.md`, not here.

- **2026-09-05** — In the context of three reopened review cycles covering the
  same change, facing two whose subject was a working-copy state amended away
  the same hour, we decided to cancel those two as superseded and retry only the
  cycle whose target is the shipped diff, and neglected retrying all three as
  reopened, because `jj evolog -r vpupoxvolrlk` shows one change amended at
  01:11:16, 01:22:00 and 01:46:32 with the cycles opened seconds after each
  (01:12:16, 01:22:55, 01:46:40), so two of the three name diffs that exist only
  in the evolog and no reviewer can be dispatched at them; accepting that
  `review cancel` records a `cancellation_actor` but has no evidence field, so
  the register says who retired them and this entry is the only place that says
  why. Cycles `0fbc856e` and `f41a7e9f` cancelled, no verdict asserted about the
  change; `db2103ce` (target `dd8988181aea`) retried separately. The group had
  been reopen-eligible but not actionable for four days: at the frozen 20.0s ctx
  budget a retry could only fail identically, until agent-review
  `mqoznwmrnnvy` added `review retry --timeout`.
