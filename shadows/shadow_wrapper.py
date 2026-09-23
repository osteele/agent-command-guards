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

import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

if os.name == "nt":
    # fcntl does not exist on Windows; lock the same file through the CRT so
    # concurrent writers still serialize there.
    import msvcrt

    def acquire_lock(lock_fd: int, offset: int = 0) -> None:
        os.lseek(lock_fd, offset, os.SEEK_SET)
        msvcrt.locking(lock_fd, msvcrt.LK_LOCK, 1)

    def release_lock(lock_fd: int, offset: int = 0) -> None:
        os.lseek(lock_fd, offset, os.SEEK_SET)
        msvcrt.locking(lock_fd, msvcrt.LK_UNLCK, 1)

    def try_acquire_lock(lock_fd: int, offset: int = 0) -> bool:
        os.lseek(lock_fd, offset, os.SEEK_SET)
        try:
            msvcrt.locking(lock_fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
else:
    import fcntl

    def acquire_lock(lock_fd: int, offset: int = 0) -> None:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

    def release_lock(lock_fd: int, offset: int = 0) -> None:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)

    def try_acquire_lock(lock_fd: int, offset: int = 0) -> bool:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

# Managed hosts that require network checks
TARGET_HOSTS = {"alpha", "beta", "gamma"}
SSH_NON_CONNECTING_OPTIONS = {"-G", "-Q", "-V"}

# State file location
STATE_DIR = Path.home() / ".cache" / "agent-command-guards"
STATE_FILE = STATE_DIR / "state.json"
LOCK_FILE = STATE_DIR / "state.lock"
DIALOG_TIMEOUT = 300
CONFIRMATION_TIMEOUT = DIALOG_TIMEOUT + 5
# Windows byte locks are mandatory: keep the owner lock outside the JSON bytes.
CONFIRMATION_LOCK_OFFSET = 4096

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
    pending_confirmation: str | None = None


@contextmanager
def state_lock():
    """Context manager for multiprocess-safe state access."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(LOCK_FILE), os.O_RDWR | os.O_CREAT)
    try:
        acquire_lock(lock_fd)
        yield
    finally:
        release_lock(lock_fd)
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
            or (
                state.pending_confirmation is not None
                and (
                    not isinstance(state.pending_confirmation, str)
                    or len(state.pending_confirmation) != 32
                    or any(c not in "0123456789abcdef" for c in state.pending_confirmation)
                )
            )
            for state in state_by_host.values()
        ):
            return {}
        return state_by_host
    except (AttributeError, json.JSONDecodeError, TypeError, KeyError):
        return {}


def save_state(state: dict[str, HostState]) -> None:
    """Save state to file.

    Written to a sibling temporary file and renamed into place, so a crash
    mid-write cannot replace the previous decisions with a truncated document.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temp_file = STATE_DIR / (STATE_FILE.name + ".tmp")
    with temp_file.open("w") as f:
        json.dump({host: asdict(s) for host, s in state.items()}, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.replace(temp_file, STATE_FILE)
    except OSError:
        temp_file.unlink(missing_ok=True)
        raise


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


def show_network_dialog(host: str) -> bool | None:
    """Return Yes, No, or None when no explicit answer could be obtained."""
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
            timeout=DIALOG_TIMEOUT,
        )
        if result.returncode == 0:
            answer = result.stdout.strip()
            if answer == "button returned:Yes":
                return True
            if answer == "button returned:No":
                return False
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


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



def record_accessible(host: str) -> None:
    """Record that host is accessible (resets declined state)."""
    with state_lock():
        state = load_state()
        previous = state.get(host)
        state[host] = HostState(
            declined=False,
            last_checked=datetime.now(timezone.utc).isoformat(),
            was_accessible=True,
            pending_confirmation=previous.pending_confirmation if previous else None,
        )
        save_state(state)


def confirmation_path(host: str, token: str) -> Path:
    return STATE_DIR / f"confirmation-{host}-{token}.json"


def remove_confirmation(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except PermissionError:
        if os.name != "nt":
            raise
        # Windows cannot unlink an open file. Each participant retries after
        # closing; a later request collects any file left by a killed process.


def read_confirmation(fd: int) -> dict:
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        result = json.loads(os.read(fd, 4096))
        if (
            isinstance(result, dict)
            and isinstance(result.get("deadline"), (float, int))
            and 0 < result["deadline"] < float("inf")
            and result.get("answer") in ("pending", "accepted", "declined", "unavailable")
        ):
            return result
    except (ValueError, UnicodeDecodeError):
        pass
    return {"deadline": 0, "answer": "unavailable"}


def write_confirmation(fd: int, confirmation: dict) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    data = json.dumps(confirmation).encode()
    os.write(fd, data)
    os.ftruncate(fd, len(data))


def finish_confirmation(
    host: str,
    token: str,
    fd: int,
    answer: str,
    state: dict[str, HostState],
) -> str:
    """Publish once under state_lock; open descriptors retain the shared answer."""
    confirmation = read_confirmation(fd)
    if confirmation["answer"] == "pending":
        confirmation["answer"] = answer
        write_confirmation(fd, confirmation)
    answer = confirmation["answer"]
    host_state = state.get(host)
    if host_state is not None and host_state.pending_confirmation == token:
        host_state.pending_confirmation = None
        if answer == "declined":
            host_state.declined = True
        save_state(state)
    remove_confirmation(confirmation_path(host, token))
    return answer


def confirmation_unavailable(fd: int, confirmation: dict) -> bool:
    """An abandoned owner or expired lease cannot leave requests waiting forever."""
    if time.monotonic() >= confirmation["deadline"]:
        return True
    if try_acquire_lock(fd, CONFIRMATION_LOCK_OFFSET):
        release_lock(fd, CONFIRMATION_LOCK_OFFSET)
        return True
    return False


def join_confirmation(host: str) -> tuple[str, int, bool] | None:
    """Atomically join a live prompt or acquire ownership of a new one."""
    with state_lock():
        state = load_state()
        host_state = state.get(host)
        show_dialog = should_show_dialog(host, is_accessible=False, state=state)
        if host_state is None:
            host_state = state[host] = HostState(False, "", False)
        host_state.was_accessible = False
        host_state.last_checked = datetime.now(timezone.utc).isoformat()
        token = host_state.pending_confirmation
        # Only our generation files are collected. Unlinking never invalidates a
        # joined request's descriptor, and a dead request leaves no retained file.
        for path in STATE_DIR.glob(f"confirmation-{host}-*.json"):
            if token is None or path != confirmation_path(host, token):
                remove_confirmation(path)
        if token is not None:
            try:
                fd = os.open(confirmation_path(host, token), os.O_RDWR)
            except FileNotFoundError:
                host_state.pending_confirmation = None
            else:
                confirmation = read_confirmation(fd)
                if confirmation["answer"] == "pending":
                    if confirmation_unavailable(fd, confirmation):
                        finish_confirmation(host, token, fd, "unavailable", state)
                    else:
                        save_state(state)
                    return token, fd, False
                # An owner may have died after writing its answer but before
                # retiring the prompt. Do not grant that answer to a later caller.
                finish_confirmation(host, token, fd, confirmation["answer"], state)
                os.close(fd)
                show_dialog = not host_state.declined
        if not show_dialog:
            save_state(state)
            return None
        token = os.urandom(16).hex()
        fd = os.open(confirmation_path(host, token), os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            acquire_lock(fd, CONFIRMATION_LOCK_OFFSET)
            write_confirmation(
                fd, {"deadline": time.monotonic() + CONFIRMATION_TIMEOUT, "answer": "pending"}
            )
            host_state.pending_confirmation = token
            save_state(state)
        except BaseException:
            os.close(fd)
            remove_confirmation(confirmation_path(host, token))
            raise
        return token, fd, True


def await_confirmation(host: str, token: str, fd: int) -> str:
    while True:
        with state_lock():
            confirmation = read_confirmation(fd)
            if confirmation["answer"] != "pending":
                return confirmation["answer"]
            if confirmation_unavailable(fd, confirmation):
                return finish_confirmation(host, token, fd, "unavailable", load_state())
        time.sleep(0.05)


def check_host(host: str, ssh_binary: str) -> bool:
    """Probe every request, sharing only the outstanding prompt's answer."""
    if probe_host(host, ssh_binary):
        record_accessible(host)
        return True

    joined = join_confirmation(host)
    answer = "declined" if joined is None else None
    if joined is not None:
        token, fd, owner = joined
        try:
            if owner:
                result = show_network_dialog(host)
                if result is True:
                    answer = "accepted"
                elif result is False:
                    answer = "declined"
                else:
                    answer = "unavailable"
                with state_lock():
                    answer = finish_confirmation(host, token, fd, answer, load_state())
            else:
                print(f"Waiting for shared confirmation for {host}.", file=sys.stderr, flush=True)
                answer = await_confirmation(host, token, fd)
        finally:
            # Closing releases the owner's advisory lock even on cancellation.
            os.close(fd)
            if answer in ("accepted", "declined", "unavailable"):
                remove_confirmation(confirmation_path(host, token))
    if answer == "accepted":
        return True
    if answer == "unavailable":
        print(f"Error: Confirmation for {host} is unavailable; connection refused.", file=sys.stderr)
    else:
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
