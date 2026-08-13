#!/usr/bin/env python3
"""
SSH/SCP/rsync wrapper with network availability dialogs.

This script wraps ssh, scp, and rsync commands. When connecting to managed hosts
(alpha, beta, gamma), it probes for connectivity first. If unreachable,
it shows a macOS dialog asking if the user wants to change their network.

Usage:
    Create symlinks to this script named 'ssh', 'scp', and 'rsync'.
    The script determines behavior based on how it was invoked (sys.argv[0]).
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

# Managed hosts that require network checks
TARGET_HOSTS = {"alpha", "beta", "gamma"}
SSH_NON_CONNECTING_OPTIONS = {"-G", "-Q", "-V"}

# State file location
STATE_DIR = Path.home() / ".cache" / "agent-command-guards"
STATE_FILE = STATE_DIR / "state.json"
LOCK_FILE = STATE_DIR / "state.lock"

# SSH options that consume the next argument
SSH_OPTIONS_WITH_VALUES = {
    "-B",
    "-b",
    "-c",
    "-D",
    "-E",
    "-e",
    "-F",
    "-I",
    "-i",
    "-J",
    "-L",
    "-l",
    "-m",
    "-O",
    "-o",
    "-p",
    "-Q",
    "-R",
    "-S",
    "-W",
    "-w",
}

# SCP options that consume the next argument
SCP_OPTIONS_WITH_VALUES = {"-c", "-D", "-F", "-i", "-J", "-l", "-o", "-P", "-S", "-X"}

# Rsync options whose values are not transfer endpoints. Long options also
# accept --option=value, which is skipped without consulting this set.
RSYNC_OPTIONS_WITH_VALUES = {
    "-B",
    "-e",
    "-f",
    "-M",
    "-T",
    "--address",
    "--backup-dir",
    "--block-size",
    "--bwlimit",
    "--checksum-seed",
    "--chmod",
    "--chown",
    "--compare-dest",
    "--compress-level",
    "--copy-dest",
    "--debug",
    "--exclude",
    "--exclude-from",
    "--files-from",
    "--filter",
    "--groupmap",
    "--iconv",
    "--include",
    "--include-from",
    "--info",
    "--link-dest",
    "--log-file",
    "--log-file-format",
    "--max-delete",
    "--max-size",
    "--min-size",
    "--modify-window",
    "--out-format",
    "--password-file",
    "--port",
    "--protocol",
    "--read-batch",
    "--remote-option",
    "--rsync-path",
    "--rsh",
    "--sockopts",
    "--suffix",
    "--temp-dir",
    "--timeout",
    "--usermap",
    "--write-batch",
}


@dataclass
class HostState:
    """State for a single managed host."""

    declined: bool  # User said "No" to dialog
    last_checked: str  # ISO timestamp
    was_accessible: bool  # Last known accessibility


@contextmanager
def state_lock():
    """Context manager for multiprocess-safe state access."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(LOCK_FILE), os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def load_state() -> dict[str, HostState]:
    """Load state from file."""
    if not STATE_FILE.exists():
        return {}
    try:
        with STATE_FILE.open() as f:
            data = json.load(f)
        state_by_host = {host: HostState(**state) for host, state in data.items()}
        if any(
            not isinstance(state.declined, bool)
            or not isinstance(state.last_checked, str)
            or not isinstance(state.was_accessible, bool)
            for state in state_by_host.values()
        ):
            return {}
        return state_by_host
    except (AttributeError, json.JSONDecodeError, TypeError, KeyError):
        return {}


def save_state(state: dict[str, HostState]) -> None:
    """Save state to file."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with STATE_FILE.open("w") as f:
        json.dump({host: asdict(s) for host, s in state.items()}, f, indent=2)


def find_real_binary(name: str) -> str:
    """Find the real binary, skipping our wrapper."""
    script_path = os.path.realpath(__file__)

    result = subprocess.run(
        ["which", "-a", name],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        print(f"Error: Could not find {name} binary", file=sys.stderr)
        sys.exit(1)

    for candidate in result.stdout.strip().split("\n"):
        if not candidate:
            continue
        real_path = os.path.realpath(candidate)
        if real_path != script_path:
            return candidate

    print(
        f"Error: Could not find real {name} binary (only found wrapper)",
        file=sys.stderr,
    )
    sys.exit(1)


def normalize_host(value: str) -> str:
    """Normalize host from user@host or [host]:port format."""
    if "@" in value:
        value = value.split("@")[-1]
    if value.startswith("["):
        closing_bracket = value.find("]")
        if closing_bracket != -1:
            value = value[1:closing_bracket]
    elif value.count(":") == 1:
        value = value.split(":", 1)[0]
    return value.rstrip(".").lower()


def extract_ssh_host(args: list[str]) -> str | None:
    """Extract target host from ssh arguments."""
    skip_next = False
    end_of_options = False

    for arg in args:
        if skip_next:
            skip_next = False
            continue

        if end_of_options:
            return normalize_host(arg)

        if arg == "--":
            end_of_options = True
            continue

        if arg.startswith("-"):
            # Check if this option consumes the next argument
            if arg in SSH_OPTIONS_WITH_VALUES:
                skip_next = True
            # Handle combined option-value like -oOption=value or -p22
            elif len(arg) > 2 and arg[:2] in SSH_OPTIONS_WITH_VALUES:
                pass  # Value is part of this arg
            continue

        # First positional argument is the host
        return normalize_host(arg)

    return None


def is_non_connecting_ssh_invocation(args: list[str]) -> bool:
    """Return whether an option before the destination makes ssh exit early."""
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg == "--":
            return False
        if not arg.startswith("-"):
            return False
        if arg in SSH_NON_CONNECTING_OPTIONS:
            return True
        if arg in SSH_OPTIONS_WITH_VALUES:
            skip_next = True
    return False


def extract_host_from_spec(spec: str) -> str | None:
    """Extract hostname from user@host:path or host:path format."""
    if spec.startswith(("rsync://", "scp://")):
        try:
            host = urlsplit(spec).hostname
        except ValueError:
            return None
        return normalize_host(host) if host else None

    # Must contain : to be a remote path
    if ":" not in spec:
        return None

    # Standard host:path or host::path format
    before_colon = spec.split(":")[0]
    if not before_colon:
        return None

    return normalize_host(before_colon)


def iterate_non_option_args_scp(args: list[str]) -> Iterator[str]:
    """Iterate over non-option arguments for scp."""
    skip_next = False

    for arg in args:
        if skip_next:
            skip_next = False
            continue

        if arg.startswith("-"):
            if arg in SCP_OPTIONS_WITH_VALUES:
                skip_next = True
            continue

        yield arg


def iterate_non_option_args_rsync(args: list[str]) -> Iterator[str]:
    """Iterate over non-option arguments for rsync.

    rsync has many options. We look for arguments that match
    host:path patterns rather than trying to parse all options.
    """
    skip_next = False
    end_of_options = False

    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if not end_of_options and arg == "--":
            end_of_options = True
            continue
        if not end_of_options and arg.startswith("-"):
            if "=" not in arg and arg in RSYNC_OPTIONS_WITH_VALUES:
                skip_next = True
            continue
        yield arg


def find_target_hosts(args: list[str], command: str) -> set[str]:
    """Find all target hosts from command arguments that we manage."""
    hosts: set[str] = set()

    if command == "ssh":
        if is_non_connecting_ssh_invocation(args):
            return hosts
        host = extract_ssh_host(args)
        if host and host in TARGET_HOSTS:
            hosts.add(host)
    elif command == "scp":
        for arg in iterate_non_option_args_scp(args):
            host = extract_host_from_spec(arg)
            if host and host in TARGET_HOSTS:
                hosts.add(host)
    elif command == "rsync":
        for arg in iterate_non_option_args_rsync(args):
            host = extract_host_from_spec(arg)
            if host and host in TARGET_HOSTS:
                hosts.add(host)

    return hosts


def probe_host(host: str, ssh_binary: str) -> bool:
    """Quick probe to check if host is reachable via SSH."""
    try:
        result = subprocess.run(
            [
                ssh_binary,
                "-o",
                "ConnectTimeout=3",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=accept-new",
                host,
                "echo",
                "ok",
            ],
            capture_output=True,
            check=False,
            timeout=10,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def show_network_dialog(host: str) -> bool:
    """Show macOS dialog asking if user wants to change network.

    Returns True if user clicked "Yes" (will change network), False for "No".
    """
    script = f"""
    display dialog "Cannot reach {host}.

Change network connection to access this host?" buttons {{"No", "Yes"}} default button "Yes" with icon caution with title "SSH Connection"
    """
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            check=False,
            text=True,
            timeout=300,
        )
        return result.returncode == 0 and "Yes" in result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def should_show_dialog(
    host: str, is_accessible: bool, state: dict[str, HostState]
) -> bool:
    """Determine if we should show the dialog for this host."""
    host_state = state.get(host)

    if host_state is None:
        return True  # Never seen this host, show dialog

    if is_accessible:
        return False  # No need for dialog

    if host_state.was_accessible and not is_accessible:
        # Host was accessible, now isn't - should show dialog
        return True

    return not host_state.declined  # Show if not declined


def record_declined(host: str) -> None:
    """Record that user declined the dialog for this host."""
    with state_lock():
        state = load_state()
        state[host] = HostState(
            declined=True,
            last_checked=datetime.now(timezone.utc).isoformat(),
            was_accessible=False,
        )
        save_state(state)


def record_accessible(host: str) -> None:
    """Record that host is accessible (resets declined state)."""
    with state_lock():
        state = load_state()
        state[host] = HostState(
            declined=False,
            last_checked=datetime.now(timezone.utc).isoformat(),
            was_accessible=True,
        )
        save_state(state)


def check_host(host: str, ssh_binary: str) -> bool:
    """Check if host is accessible, handling dialog if needed.

    Returns True if we should proceed with the command, False to abort.
    """
    is_accessible = probe_host(host, ssh_binary)

    if is_accessible:
        record_accessible(host)
        return True

    # Host not accessible - check if we should show dialog
    with state_lock():
        state = load_state()
        show_dialog = should_show_dialog(host, is_accessible=False, state=state)

        if show_dialog:
            # Update state to show we're checking
            if host not in state:
                state[host] = HostState(
                    declined=False,
                    last_checked=datetime.now(timezone.utc).isoformat(),
                    was_accessible=False,
                )
            else:
                state[host].was_accessible = False
                state[host].last_checked = datetime.now(timezone.utc).isoformat()
            save_state(state)

    if show_dialog:
        if show_network_dialog(host):
            # User said "Yes" - they'll change network, proceed
            return True
        else:
            # User said "No"
            record_declined(host)
            print(
                f"Error: {host} is not accessible.\n"
                f"The host will not be available until you change your network settings.\n"
                f"Note: Using other SSH options or ping will not help.",
                file=sys.stderr,
            )
            return False
    else:
        # Dialog was previously declined
        print(
            f"Error: {host} is not accessible.\n"
            f"The host will not be available until you change your network settings.\n"
            f"Note: Using other SSH options or ping will not help.",
            file=sys.stderr,
        )
        return False


def main() -> int:
    """Main entry point."""
    # Determine which command we're wrapping
    command = Path(sys.argv[0]).name

    if command not in ("ssh", "scp", "rsync"):
        print(f"Error: Unknown command '{command}'", file=sys.stderr)
        return 1

    # Find real binary
    real_binary = find_real_binary(command)
    args = sys.argv[1:]

    # Find target hosts
    target_hosts = find_target_hosts(args, command)

    if not target_hosts:
        # No managed hosts, pass through
        os.execv(real_binary, [command] + args)

    # Get SSH binary for probing (might be different from command)
    if command == "ssh":
        ssh_binary = real_binary
    else:
        ssh_binary = find_real_binary("ssh")

    # Check each managed host
    for host in target_hosts:
        if not check_host(host, ssh_binary):
            return 1

    # All hosts accessible or user approved proceeding
    os.execv(real_binary, [command] + args)


if __name__ == "__main__":
    sys.exit(main())
