# Agent Command Guards

Command wrappers that intercept `ssh`, `scp`, `rsync`, `git`, and `uv` to apply
agent-specific workstation policies.

These wrappers enforce policy at the executable boundary. Agent launchers can
put the directory first on `PATH`, while hooks can call individual guards by
absolute path. Provider selection, agent permissions, and tool-request policy
belong to the launchers and hooks instead.

## Installation

Add this directory to the beginning of your PATH:

```bash
export PATH="$HOME/code/agent-tools/agent-command-guards:$PATH"
```

`claude-wrapper` can prepend the directory through its `prepend_path` setting.
`codex-wrapper` prepends it automatically and verifies the installation with
`codex wrapper doctor`.

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

The selected ceiling is printed when the command starts. Set
`LLM_RAM_GUARD_QUIET=1` to suppress that line. The snapshot is intentionally
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
