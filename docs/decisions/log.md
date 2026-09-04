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
