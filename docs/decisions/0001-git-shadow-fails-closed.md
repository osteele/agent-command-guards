---
status: accepted
date: 2026-09-04
---

# 0001. The git shadow refuses unclassified subcommands

## Context and Problem Statement

In a repository where jj and Git are co-located, jj owns the working copy and the
operation log while Git's `HEAD` sits detached at the working-copy parent. A Git
command that mutates the index, `HEAD`, or refs desynchronizes that arrangement,
and the damage surfaces later — in a `jj log` that disagrees with the files on
disk, or a revision that cannot be described — far from the command that caused
it.

Agents reach for Git constantly, from training and from habit, so the `git`
shadow exists to intercept. The question this record settles is what it does
with a subcommand it does not recognize, which is the common case: Git has
roughly 150 subcommands and this wrapper classifies a few dozen.

## Decision Outcome

Refuse. In a co-located jj repository, only explicitly allowlisted read-oriented
commands and the deliberate translations (`add`, `commit`, `worktree`) run; every
unclassified subcommand exits non-zero with a message to use jj instead.
Repositories without a `.jj` directory are untouched.

Classification is by enumeration, not by pattern: a command is safe because
someone decided it was, not because its name resembled one that is.

### Consequences

- Every Git subcommand is refused until someone classifies it, including
  harmless ones. That friction is the mechanism, not a side effect.
- **It breaks tools that shell out to Git, including jj itself.** `jj git fetch`
  against a local-path remote invokes the external `git` binary, which resolves
  to this shadow and is refused: `External git program failed`. Observed
  2026-09-04 while building cross-repo transport for research-site, where it
  blocked the merge-back path entirely until diagnosed. The fix is an explicit
  transport allowlist — `fetch`, `push`, `ls-remote`, and the pack helpers, which
  move objects and refs without touching the working copy or index — and not a
  relaxation of the default.
- A contributor meeting a refusal for a command they believe is safe can add it
  to the allowlist without establishing whether it mutates. The enumeration is
  only as good as the judgement behind each entry.
- Diagnosing a refusal costs a round trip when the caller is a subprocess rather
  than a person, because the failure appears as the parent tool's error.

## Considered Options

### Delegate unclassified commands to Git

What a compatibility wrapper normally does, and what makes it invisible when it
has nothing to say.

Rejected because the set of Git commands that quietly mutate state is large,
grows with each Git release, and includes commands whose names suggest
inspection. Defaulting to delegation means the wrapper protects against exactly
the commands someone remembered, and a single `git checkout` or `git reset`
reaching Git leaves a working copy jj will later report as a surprise.

### Warn and delegate

Print the jj alternative, then run the Git command anyway.

Rejected because a warning that does not block is indistinguishable from success
in a transcript: the mutation has already happened by the time anyone reads the
line, and an agent that did not ask for the advice has no reason to act on it.

## More Information

- **References**: `README.md` § git for the allowlist and the translations
