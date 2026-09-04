# Decision records

Numbered records of decisions that constrain this repository, each stating one
decision, the forces behind it, the alternatives rejected, and what it costs.
Consult these before changing what they constrain.

Every file here is on `PATH`-adjacent territory: a wrapper that misbehaves
breaks the tool calls of every live agent session, which is why several of the
decisions below are deliberate absences rather than features.

## The gate

A decision is recorded only if the alternative that lost can be named, and
credible means someone with the same information and the same intent could have
chosen it — not a strawman, not an option invented while writing the record. The
cost is named too; a record listing only benefits is not one a reader can act
on. Failing the gate means writing nothing, not writing something shorter.

## Two tiers

Depth follows reversibility, not importance.

| Shape | Tier |
| --- | --- |
| Hard to reverse, or a deliberate absence | a numbered record here |
| Everything else that passes the gate | a one-line entry in [log.md](log.md) |

A log entry may be promoted to a full record later, keeping its original
wording, when the decision turns out to constrain more than it looked like it
would.

Prefer an enforceable invariant to a record. Where a test can fail while the
maintainer is still working, that is stronger protection than prose, and the
record is redundant.

## Immutability

A record describes what was decided when it was decided; it is not
current-state documentation. A change of position is a new record naming the one
it supersedes. A record that says something untrue is corrected in place, so the
next reader meets the corrected version rather than a wrong one plus a
correction. For how the wrappers behave today, read `README.md`.

## Records

None yet. Candidates are listed in [log.md](log.md) where they were recorded at
the lighter tier.
