# Agent Command Guards

Command wrappers that intercept `ssh`, `scp`, `rsync`, `git`, and `uv` to apply
agent-specific workstation policies.

These wrappers enforce policy at the executable boundary. Agent launchers can
put the directory first on `PATH`, while hooks can call individual guards by
absolute path. Provider selection, agent permissions, and tool-request policy
belong to the launchers and hooks instead.

## Installation

This directory belongs on `PATH` only inside an agent session. Putting it on
`PATH` for ordinary shells shadows `uv` for every command the user runs, which
floods tools that invoke `uv run` per file -- `jj fix` is the usual casualty.

Each agent gets there through its own launcher:

| Agent | Launcher |
| --- | --- |
| Claude Code | `claude-wrapper`, via its `prepend_path` setting |
| Codex | `codex-wrapper`, verified with `codex wrapper doctor` |
| Kimi, opencode | `agent-launcher` in this repository (see below) |

## Agent launchers

`agent-launcher` is a generic launcher invoked through a symlink named for the
agent it starts. The symlink name selects the real binary to resolve and whether
that agent needs the Zsh bridge; the rest is shared.

```bash
./launchers/setup            # install
./launchers/setup --dry-run  # preview
./launchers/setup --uninstall
```

Setup links `~/bin/kimi` and `~/bin/opencode` to the launchers and adds a
managed block to `~/.zshenv` and `~/.bashrc` that prepends `launchers/` to
`PATH`. That subdirectory holds only the launchers, so making it globally
visible does not make the command shadows globally visible. Verify with:

```bash
kimi wrapper doctor
opencode wrapper doctor
```

Adding another agent takes a symlink in `launchers/` plus, if its installer puts
the binary somewhere a login shell would not find, an entry in the launcher's
`fallback_candidates`.

### The Zsh bridge

`launchers/shell-init` is a private `ZDOTDIR` whose startup files source the
user's own configuration and then restore the shadow directory to the front of
`PATH`. Agents need it only if they re-source shell configuration for their
shell tool, because doing so puts mise's `uv` shim back ahead of the shadows.

- `opencode` takes a shell snapshot that sources `${ZDOTDIR:-$HOME}/.zshrc`, so
  it gets the bridge.
- `kimi` runs tool commands through `sh -c`, which reads no startup files, so it
  inherits `PATH` directly and does not.

The bridge covers Zsh only; an agent that snapshots Bash would need its own.

## Commands

### ssh, scp, rsync

Python wrappers (symlinks to `shadow_wrapper.py`) that check connectivity to managed hosts before connecting.

**Managed hosts:** `alpha`, `beta`, `gamma`

**Behavior:**
1. Parse command arguments to identify target hosts
2. If target is a managed host, probe connectivity via SSH
3. If accessible: proceed with the command
4. If not accessible:
   - Show a macOS dialog asking if user wants to change network
   - "Yes": proceed anyway (user will change network)
   - "No": print error message and exit
5. Remember "No" decisions per host until the host becomes accessible again

**State file:** `~/.cache/agent-command-guards/state.json`

### git

Bash wrapper that detects when a project uses both Git and [Jujutsu](https://github.com/martinvonz/jj) version control, and reminds you to use `jj` instead.
For read-oriented commands and branch inspection, it exports jj state and points
Git's `HEAD` at a synthetic `jj-head` branch for the jj working-copy parent.
In jj repositories, `git worktree` add/list/remove/prune operations map to jj
workspace operations. Removal resolves the exact registered workspace path and
refuses dirty workspaces unless `-f` is supplied.

### uv and ram-guard

The `uv` shadow delegates ordinary uv subcommands unchanged. It runs `uv run`
under `ram-guard`, which monitors aggregate resident memory for the complete
process tree and terminates only its owned process group when it reaches the
limit. At each launch, the default ceiling is 70% of the memory that the host
currently reports as available. This leaves the other 30% of currently
available memory, plus memory already used by the rest of the system, outside
the new process tree's budget. Settings:

- `LLM_RAM_GUARD_AVAILABLE_FRACTION=0.70`
- `LLM_RAM_GUARD_LIMIT=8G` replaces the dynamic calculation with a fixed limit
- `PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.7`
- `PYTORCH_MPS_LOW_WATERMARK_RATIO=0.6`

Existing PyTorch variables take precedence. Customize the defaults with
`LLM_MPS_HIGH_WATERMARK_RATIO` and `LLM_MPS_LOW_WATERMARK_RATIO`, or bypass one
invocation explicitly:

```bash
LLM_RAM_GUARD=off uv run python large-intentional-job.py
```

The selected ceiling is printed when the command starts, but only when stderr is
a terminal, so tools that capture stderr per invocation (`jj fix` runs a
formatter once per file per revision) are not flooded with banners. Set
`LLM_RAM_GUARD_QUIET=1` to suppress the line. The snapshot is intentionally
taken immediately before launch; it cannot reserve memory against unrelated
processes that grow later.

On macOS, the snapshot comes from `memory_pressure -Q`; Linux uses
`MemAvailable` from `/proc/meminfo`. The guard enforces aggregate process-tree
RSS when process inspection is permitted. Restricted sandboxes that block
process inspection use the corresponding available-memory floor.

The Claude/Codex shared pre-tool hook also wraps parsed `uv run` commands by an
absolute guard path, covering absolute uv paths that bypass the shadow.

## Examples

```bash
# Connects normally if beta is reachable
ssh beta

# Shows dialog if alpha is unreachable
scp file.txt alpha:/path/to/dest

# Probes host before syncing
rsync -av local/ gamma:/remote/

# Reminds you about jj if .jj directory exists
git status  # "Note: This project uses jj..."
```

## How It Works

The wrappers use `which -a` to find the real binary, skipping themselves via `realpath()` comparison. For ssh/scp/rsync:

- **SSH**: Extracts host from first positional argument (after parsing options like `-p`, `-o`)
- **SCP/rsync**: Scans transfer endpoints in `[user@]host:path`, `scp://`, and
  `rsync://` forms

Network probing uses:
```bash
ssh -o ConnectTimeout=3 -o BatchMode=yes <host> echo ok
```

The macOS dialog uses `osascript`:
```applescript
display dialog "Cannot reach <host>..." buttons {"No", "Yes"}
```

## State Management

State is stored in JSON format with file locking (`fcntl.flock`) for multiprocess safety:

```json
{
  "beta": {
    "declined": false,
    "last_checked": "2025-12-30T14:23:51.657123+00:00",
    "was_accessible": true
  }
}
```

When a host becomes accessible again, the `declined` flag is automatically reset.
