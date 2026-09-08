# Agent Command Guards

[![CI](https://github.com/osteele/agent-command-guards/actions/workflows/ci.yml/badge.svg)](https://github.com/osteele/agent-command-guards/actions/workflows/ci.yml)

Wrappers for `ssh`, `scp`, `rsync`, `git`, and `uv` that apply workstation policy
to the commands an agent runs. Each wrapper does its work and hands off to the
real binary, so a guarded command behaves like the unguarded one everywhere the
policy has nothing to say.

Policy applies at the executable boundary, which catches a command however it was
composed. Provider selection, agent permissions, and tool-request policy belong to
the launchers and to [agent-tool-policy](https://github.com/osteele/agent-tool-policy), the shared pre-tool
hook that also calls `with-limits` here by absolute path.

## Layout

`shadows/` holds the wrappers and is the only directory that goes on `PATH`. It
contains exactly what should become a command name. The repository root used to be
the `PATH` entry, which meant any file added there became a command in every agent
session, so a helper named `setup` or `check` would have shadowed the real one.

`launchers/` follows the same rule for the agent launchers. `tests/`,
`agent-launcher`, and the documentation stay off `PATH`.

## Installation

`shadows/` belongs on `PATH` inside an agent session only. In an ordinary shell it
shadows `uv` for every command the user runs, which floods tools that invoke
`uv run` once per file. `jj fix` is the usual casualty.

Each agent gets there through its own launcher:

| Agent | Launcher |
| --- | --- |
| Claude Code | `claude-wrapper`, via its `prepend_path` setting |
| Codex, kimi, opencode, OMP | `agent-launcher` in this repository (see below) |

Confirm inside a session that the guards win the lookup:

```bash
command -v git   # want …/agent-command-guards/shadows/git, not /usr/bin/git
```

Being on `PATH` is not enough, and the two ways it falls short are both silent. A
`prepend_path` value written in tilde form (`~/code/...`) enters `PATH` literally
and never expands. A value that does expand can still land behind `/usr/bin`,
because an agent that re-sources shell configuration for its shell tool lets mise,
pixi, and other version managers prepend themselves afterwards. Either way every
command resolves to the system binary while the directory still appears in `PATH`,
so `command -v` is the only check that means anything.

`launchers/setup` handles the second case. It writes
`~/.config/agent-launchers/env`, sourced from a managed block at the end of
`.zshenv`, `.zshrc`, and `.bashrc`, which moves the guards directory back to the
front whenever it is already on `PATH`. An ordinary shell has no guards directory
on `PATH`, so the block does nothing there.

## Agent launchers

`agent-launcher` is a generic launcher invoked through a symlink named for the
agent it starts. The symlink name selects the real binary to resolve and whether
that agent needs the Zsh bridge; the rest is shared.

```bash
./launchers/setup            # install
./launchers/setup --dry-run  # preview
./launchers/setup --uninstall
```

Setup links `~/bin/kimi`, `~/bin/opencode`, `~/bin/codex`, and `~/bin/omp` to the launchers,
and adds a managed block to `~/.zshenv`, `~/.zshrc`, and `~/.bashrc` that prepends
`launchers/` to `PATH` and restores the guards to the front of it. That
subdirectory holds only the launchers, so making it globally visible does not make
the command shadows globally visible. Verify with:

```bash
kimi wrapper doctor
opencode wrapper doctor
codex wrapper doctor
omp wrapper doctor
```

Adding another agent takes a symlink in `launchers/` plus, if its installer puts
the binary somewhere a login shell would not find, an entry in the launcher's
`fallback_candidates`.

### Model-first interactive commands

`launchers/agent-model` lets an interactive shell select a model first and a
harness second. Source `shell/agent-models.sh` from the interactive shell setup
to define `claude`, `fable`, `codex`, `kimi`, and `glm` as shell functions. The
functions are not inherited by subprocesses, so a program that runs `codex
exec` still reaches the native Codex launcher.

Choose a harness for one invocation with `--harness` or `-h`:

```bash
codex                         # use the configured default harness
codex --harness codex         # use Codex itself
codex -h self                 # same: use this model's native harness
claude -h omp                 # use OMP with the current Opus selector
codex -h                      # pass -h through to the selected harness
```

The short form is consumed only when the next word is a known harness. A bare
`-h`, or `-h` followed by any other word, remains the selected harness's help
option. `--harness` reports an unknown value as an error.

Defaults come from `agent-models.toml` at the repository root. Each command
names its native harness as `home` — what `--harness self` selects — the harness
an unqualified invocation uses as `default`, and one entry per supported
`(command, harness)` pair:

```toml
version = 1

[commands.glm]
home = "opencode"
default = "omp"
routes.opencode = { model = "zai-coding-plan/glm-5.3-flash" }
routes.omp = { model = "zai/glm-5.3-flash" }
```

A route carries a `model`; how each harness spells it — `--model=X`, `--model X`,
`-m X` — is the harness's API and stays in the code.
An empty table is a harness that needs no extra arguments. `home` and `default`
must name routes that exist, which is checked at load, so a bad edit fails on the
next command rather than resolving to something unintended.

Machine-local overrides live in
`${XDG_CONFIG_HOME:-$HOME/.config}/agent-models/config.toml`, which selects a
harness per command and nothing else:

```toml
version = 1

[defaults]
claude = "claude"
fable = "claude"
```

The CLI reads and writes that file only; `agent-models.toml` is hand-edited:

```bash
agent-model default list
agent-model default get codex
agent-model default set glm opencode
agent-model default reset glm
agent-model resolve codex -h self
agent-model config path
agent-model config check
agent-model doctor
```

Every command ships defaulting to OMP. The native Claude routes resolve `claude`
through `PATH`, so an installed `claude-wrapper` remains responsible for profiles
and command guards. The OMP routes use provider-qualified model selectors for
Anthropic, Fable, Codex, Kimi Code, and the Z.AI coding plan.

### CPU priority

The launcher starts the agent under `nice -n 5`, which the agent's whole process
tree inherits. Niceness only arbitrates contention — an otherwise-idle machine
runs the agent at full speed — so delegated work yields to interactive use and
costs nothing the rest of the time. Set `AGENT_LAUNCHER_NICE` to change the
value (`0` disables).

### Codex open-file limit

Codex loads skills concurrently. Its current loader can exceed macOS's default
soft limit of 256 file descriptors and then skip valid skills with `Too many
open files (os error 24)`. The Codex launcher raises a lower inherited soft
limit to 65,536 before starting the binary. It leaves the hard limit unchanged.

### Session identity

The launcher exports `AGENT_SESSION_ID`, a fresh id per launch, and unsets
`CLAUDE_CODE_SESSION_ID` and `CODEX_THREAD_ID` first.

Claude Code and Codex export a per-session id into their shell subprocesses. Kimi
and opencode export none. OMP exposes its native conversation id to extensions,
but not to MCP or tool subprocesses, so its push bridge, tools, and Weft jobs
otherwise acquire different identities. Addressing agent-mail to one session
rather than broadcasting to the whole project is the case that motivated this.
`AGENT_SESSION_ID` fills the gap.

The unset handles nesting. An agent started from inside another agent's shell
inherits that parent's session id, and answering to it would attribute this
session's work to the parent. Minting unconditionally does the same for an
inherited `AGENT_SESSION_ID`.

### Provider credentials

The launcher unsets `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` before starting
`omp`. Set `AGENT_LAUNCHER_KEEP_API_KEYS=1` to keep them.

OMP treats a key it finds in the environment as being logged in: the provider
list shows the provider as `logged in (env)` rather than `(login)`, and requests
bill at API rates instead of against the subscription. Authorizing the
subscription afterwards does not displace it, because an environment key leaves
the stored OAuth credential unused unless one is explicitly selected. Claude Code
asks before using a key it did not obtain itself; OMP does not ask. On a machine
that loads keys from a keychain into every interactive shell, that turns a
subscription session into a metered one with nothing on screen to say so.

`OPENAI_API_KEY` was set in every agent session when this was measured on
2026-09-05, so OMP routed to `openai-codex` was billing per token for the same
reason Anthropic would have been. Z.AI is deliberately absent: its only
available key bills the GLM coding plan, so a key there *is* the subscription.

The unset covers `omp` alone. The other launched agents authenticate elsewhere
and have no reason to lose the keys.

### The Zsh bridge

`launchers/shell-init` is a private `ZDOTDIR` whose startup files source the
user's own configuration and then restore the shadow directory to the front of
`PATH`. Agents need it only if they re-source shell configuration for their shell
tool, because doing so puts mise's `uv` shim back ahead of the shadows.

- `opencode` takes a shell snapshot that sources `${ZDOTDIR:-$HOME}/.zshrc`, and
  `codex` re-sources configuration the same way, so both get the bridge.
- `kimi` runs tool commands through `sh -c`, which reads no startup files, so it
  inherits `PATH` directly and does not.

The bridge covers Zsh only. An agent that snapshots Bash would need its own.

## Commands

### ssh, scp, rsync

Python wrappers, all three symlinks to `shadow_wrapper.py`, that check
connectivity to a managed host before connecting. The managed hosts are `alpha`,
`beta`, and `gamma`; any other target passes straight through with no probe.

```bash
ssh beta                        # connects normally when beta is reachable
scp file.txt alpha:/path/dest    # asks first when alpha is unreachable
rsync -av local/ gamma:/remote/ # probes before syncing
```

The wrapper parses the arguments for target hosts, probes a managed one with
`ssh -o ConnectTimeout=3 -o BatchMode=yes <host> echo ok`, and proceeds when the
host answers. When it does not answer, a macOS dialog asks whether to continue.
"Yes" proceeds, on the assumption the user is about to change networks. "No"
prints an error and exits.

A "No" is remembered per host, so a second attempt fails immediately instead of
asking again. The decision is cleared as soon as a probe succeeds. A host that was
reachable and no longer is gets a fresh dialog rather than the remembered answer,
since that pattern indicates a network change rather than a standing decision.

State lives in `~/.cache/agent-command-guards/state.json`, written under
`fcntl.flock` so concurrent agents do not corrupt it:

```json
{
  "beta": {
    "declined": false,
    "last_checked": "2025-12-30T14:23:51.657123+00:00",
    "was_accessible": true
  }
}
```

### git

A Bash wrapper for projects that use both Git and
[Jujutsu](https://github.com/martinvonz/jj). In a co-located repository it uses
an explicit compatibility whitelist: known read-only Git commands and the
translations below are supported, while every unclassified Git command is
refused with a reminder to use `jj`. Commands outside jj repositories continue
to use Git normally.

Allowlisted read-oriented commands and read-only branch inspection get real jj
state: the wrapper first asks jj to snapshot and synchronize the co-located
working copy. jj keeps Git's `HEAD` detached at the working-copy parent, so
`git log` and `git status` describe the repository as jj sees it rather than
stale Git state, without creating a synthetic jj bookmark. Mutating branch
forms and commands such as `git checkout`, `git reset`, and `git clean` are
refused instead of falling through to Git.

`git worktree` add, list, remove, and prune map to the corresponding `jj
workspace` operations. Removal resolves the exact registered workspace path and
refuses to touch the primary workspace, the current one, a symlink, or a path that
is merely similar. A workspace holding changes or untracked files survives unless
`-f` is supplied.

### uv and with-limits

The `uv` shadow passes ordinary uv subcommands through unchanged. It runs `uv run`
under `with-limits`, which watches the resident memory of the whole process tree and
terminates only its own process group on reaching the limit.

`with-limits` accepts either an argument-vector command after `--` or a shell command
with `-c` (short for `-- /bin/zsh -c`):

```bash
with-limits -- uv run python experiment.py
with-limits -c 'just format && uv run python -m unittest && just check'
```

The former `ram-guard` name remains as a compatibility alias.

The default ceiling is 70% of the memory the host reports as available at launch.
The remaining 30%, plus everything already in use by the rest of the system, stays
outside the new tree's budget. The snapshot is taken immediately before launch and
cannot reserve memory against unrelated processes that grow later.

| Variable | Effect |
| --- | --- |
| `LLM_RAM_GUARD_AVAILABLE_FRACTION` | Fraction of available memory to grant (default `0.70`) |
| `LLM_RAM_GUARD_LIMIT` | A fixed limit such as `8G`, replacing the dynamic calculation |
| `LLM_RAM_GUARD_QUIET` | Suppress the startup banner |
| `LLM_RAM_GUARD=off` | Skip the guard for one invocation |
| `LLM_MPS_HIGH_WATERMARK_RATIO` | PyTorch MPS hard watermark (default `0.7`) |
| `LLM_MPS_LOW_WATERMARK_RATIO` | PyTorch MPS soft watermark (default `0.6`) |

`PYTORCH_MPS_HIGH_WATERMARK_RATIO` and `PYTORCH_MPS_LOW_WATERMARK_RATIO` already
in the environment take precedence over the defaults set here.

```bash
LLM_RAM_GUARD=off uv run python large-intentional-job.py
```

The chosen ceiling is announced at startup only when stderr is a terminal, so
tools that capture stderr per invocation are not flooded. `jj fix` runs a
formatter once per file per revision and would otherwise produce a banner each
time.

Memory comes from `memory_pressure -Q` on macOS and from `MemAvailable` in
`/proc/meminfo` on Linux. The guard enforces aggregate process-tree RSS wherever
process inspection is permitted, and falls back to an available-memory floor in
sandboxes that block it.

The shared pre-tool hook in agent-tool-policy wraps `uv run` a second way, by
rewriting the command to an absolute `with-limits` path. That covers absolute uv
paths and `mise`/`command` prefixes, which never consult `PATH` and so never reach
this shadow.

## How the wrappers find the real binary

Each wrapper has to locate the command it shadows without re-executing itself.
`shadow_wrapper.py` and `git` scan `which -a <name>` and skip any candidate whose
`realpath()` matches their own. `uv` walks `PATH` by hand instead, because it also
has to skip mise shims: a shim ahead of the concrete `uv` binary hangs or recurses
when this shadow comes earlier on `PATH`.

Handoff uses `exec`, which preserves exit codes and signal behavior. `with-limits`
is the deliberate exception, staying resident to monitor its child.

## Tests

```bash
python3 -m unittest          # from the repository root
```

The suite uses `unittest`. It drives the real wrappers as subprocesses against
temporary repositories and fake binaries, so it exercises the files that agents
actually run.

CI runs it on Linux, macOS, and Windows across Python 3.11–3.14. Windows runs
the portable suites (parsers, state, size parsing, mocked monitors); suites that
need POSIX shells, `which(1)`, process groups, or memory monitors skip there
with their reason. Linux and macOS jobs install `jj` from its latest release so
the git-shadow tests run against a real repository backend.
