# Agent Command Guards

Wrappers for `ssh`, `scp`, `rsync`, `git`, and `uv` that apply workstation policy
to the commands an agent runs. Each wrapper does its work and hands off to the
real binary, so a guarded command behaves like the unguarded one everywhere the
policy has nothing to say.

Policy applies at the executable boundary, which catches a command however it was
composed. Provider selection, agent permissions, and tool-request policy belong to
the launchers and to [agent-tool-policy](https://github.com/osteele/agent-tool-policy), the shared pre-tool
hook that also calls `ram-guard` here by absolute path.

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
| Codex, kimi, opencode | `agent-launcher` in this repository (see below) |

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

Setup links `~/bin/kimi`, `~/bin/opencode`, and `~/bin/codex` to the launchers,
and adds a managed block to `~/.zshenv`, `~/.zshrc`, and `~/.bashrc` that prepends
`launchers/` to `PATH` and restores the guards to the front of it. That
subdirectory holds only the launchers, so making it globally visible does not make
the command shadows globally visible. Verify with:

```bash
kimi wrapper doctor
opencode wrapper doctor
codex wrapper doctor
```

Adding another agent takes a symlink in `launchers/` plus, if its installer puts
the binary somewhere a login shell would not find, an entry in the launcher's
`fallback_candidates`.

### Session identity

The launcher exports `AGENT_SESSION_ID`, a fresh id per launch, and unsets
`CLAUDE_CODE_SESSION_ID` and `CODEX_THREAD_ID` first.

Claude Code and Codex export a per-session id into their shell subprocesses. Kimi
and opencode export none, so a tool that needs to tell two sessions in one
directory apart has nothing to go on. Addressing agent-mail to one session rather
than broadcasting to the whole project is the case that motivated this.
`AGENT_SESSION_ID` fills the gap.

The unset handles nesting. An agent started from inside another agent's shell
inherits that parent's session id, and answering to it would attribute this
session's work to the parent. Minting unconditionally does the same for an
inherited `AGENT_SESSION_ID`.

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
[Jujutsu](https://github.com/martinvonz/jj). In a repository with a `.jj`
directory it prints a reminder to use `jj`, and for several subcommands it does
more than remind.

Read-oriented commands and branch inspection get real jj state: the wrapper runs
`jj git export` and points Git's `HEAD` at a synthetic `local/jj-shadow-head` branch tracking
the jj working-copy parent, so `git log` and `git status` describe the repository
as jj sees it rather than a stale export.

`git worktree` add, list, remove, and prune map to the corresponding `jj
workspace` operations. Removal resolves the exact registered workspace path and
refuses to touch the primary workspace, the current one, a symlink, or a path that
is merely similar. A workspace holding changes or untracked files survives unless
`-f` is supplied.

### uv and ram-guard

The `uv` shadow passes ordinary uv subcommands through unchanged. It runs `uv run`
under `ram-guard`, which watches the resident memory of the whole process tree and
terminates only its own process group on reaching the limit.

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
rewriting the command to an absolute `ram-guard` path. That covers absolute uv
paths and `mise`/`command` prefixes, which never consult `PATH` and so never reach
this shadow.

## How the wrappers find the real binary

Each wrapper has to locate the command it shadows without re-executing itself.
`shadow_wrapper.py` and `git` scan `which -a <name>` and skip any candidate whose
`realpath()` matches their own. `uv` walks `PATH` by hand instead, because it also
has to skip mise shims: a shim ahead of the concrete `uv` binary hangs or recurses
when this shadow comes earlier on `PATH`.

Handoff uses `exec`, which preserves exit codes and signal behavior. `ram-guard`
is the deliberate exception, staying resident to monitor its child.

## Tests

```bash
python3 -m unittest          # from the repository root
```

The suite uses `unittest`. It drives the real wrappers as subprocesses against
temporary repositories and fake binaries, so it exercises the files that agents
actually run.
