"""Landlock LSM sandbox for host-mode agents.

Applies kernel-level filesystem restrictions so host-mode agents can only
access their scratch directory (read-write) and a set of read-only system
paths. This is a one-way ratchet: once applied, the process (and all its
children) can never regain wider access.

Requires Linux ≥ 5.13 with Landlock enabled.

Usage:
    from enclave.orchestrator.landlock import apply_sandbox, is_supported

    if is_supported():
        apply_sandbox(
            scratch_dir="/data/enclave/workspaces/my-agent",
            readonly_paths=["/usr", "/nix", "/etc"],
        )
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import struct
import sys
from pathlib import Path

from enclave.common.logging import get_logger

log = get_logger("landlock")

# ---------------------------------------------------------------------------
# Syscall numbers (x86_64)
# ---------------------------------------------------------------------------
_ARCH = os.uname().machine
if _ARCH == "x86_64":
    _SYS_LANDLOCK_CREATE_RULESET = 444
    _SYS_LANDLOCK_ADD_RULE = 445
    _SYS_LANDLOCK_RESTRICT_SELF = 446
elif _ARCH == "aarch64":
    _SYS_LANDLOCK_CREATE_RULESET = 444
    _SYS_LANDLOCK_ADD_RULE = 445
    _SYS_LANDLOCK_RESTRICT_SELF = 446
else:
    _SYS_LANDLOCK_CREATE_RULESET = 0
    _SYS_LANDLOCK_ADD_RULE = 0
    _SYS_LANDLOCK_RESTRICT_SELF = 0

# ---------------------------------------------------------------------------
# Landlock ABI constants
# ---------------------------------------------------------------------------

# landlock_ruleset_attr.handled_access_fs (ABI v1 flags)
LANDLOCK_ACCESS_FS_EXECUTE = 1 << 0
LANDLOCK_ACCESS_FS_WRITE_FILE = 1 << 1
LANDLOCK_ACCESS_FS_READ_FILE = 1 << 2
LANDLOCK_ACCESS_FS_READ_DIR = 1 << 3
LANDLOCK_ACCESS_FS_REMOVE_DIR = 1 << 4
LANDLOCK_ACCESS_FS_REMOVE_FILE = 1 << 5
LANDLOCK_ACCESS_FS_MAKE_CHAR = 1 << 6
LANDLOCK_ACCESS_FS_MAKE_DIR = 1 << 7
LANDLOCK_ACCESS_FS_MAKE_REG = 1 << 8
LANDLOCK_ACCESS_FS_MAKE_SOCK = 1 << 9
LANDLOCK_ACCESS_FS_MAKE_FIFO = 1 << 10
LANDLOCK_ACCESS_FS_MAKE_BLOCK = 1 << 11
LANDLOCK_ACCESS_FS_MAKE_SYM = 1 << 12

# ABI v2 additions
LANDLOCK_ACCESS_FS_REFER = 1 << 13

# ABI v3 additions
LANDLOCK_ACCESS_FS_TRUNCATE = 1 << 14

# Combined masks
_ALL_FS_V1 = (
    LANDLOCK_ACCESS_FS_EXECUTE
    | LANDLOCK_ACCESS_FS_WRITE_FILE
    | LANDLOCK_ACCESS_FS_READ_FILE
    | LANDLOCK_ACCESS_FS_READ_DIR
    | LANDLOCK_ACCESS_FS_REMOVE_DIR
    | LANDLOCK_ACCESS_FS_REMOVE_FILE
    | LANDLOCK_ACCESS_FS_MAKE_CHAR
    | LANDLOCK_ACCESS_FS_MAKE_DIR
    | LANDLOCK_ACCESS_FS_MAKE_REG
    | LANDLOCK_ACCESS_FS_MAKE_SOCK
    | LANDLOCK_ACCESS_FS_MAKE_FIFO
    | LANDLOCK_ACCESS_FS_MAKE_BLOCK
    | LANDLOCK_ACCESS_FS_MAKE_SYM
)

_READ_ONLY = (
    LANDLOCK_ACCESS_FS_EXECUTE
    | LANDLOCK_ACCESS_FS_READ_FILE
    | LANDLOCK_ACCESS_FS_READ_DIR
)

_READ_WRITE = (
    _READ_ONLY
    | LANDLOCK_ACCESS_FS_WRITE_FILE
    | LANDLOCK_ACCESS_FS_REMOVE_DIR
    | LANDLOCK_ACCESS_FS_REMOVE_FILE
    | LANDLOCK_ACCESS_FS_MAKE_DIR
    | LANDLOCK_ACCESS_FS_MAKE_REG
    | LANDLOCK_ACCESS_FS_MAKE_SOCK
    | LANDLOCK_ACCESS_FS_MAKE_FIFO
    | LANDLOCK_ACCESS_FS_MAKE_SYM
)

# Rule types
LANDLOCK_RULE_PATH_BENEATH = 1

# prctl
_PR_SET_NO_NEW_PRIVS = 38

# ---------------------------------------------------------------------------
# ctypes / libc helpers
# ---------------------------------------------------------------------------

_libc_path = ctypes.util.find_library("c")
_libc: ctypes.CDLL | None = None


def _get_libc() -> ctypes.CDLL:
    global _libc
    if _libc is None:
        if not _libc_path:
            raise RuntimeError("Cannot find libc")
        _libc = ctypes.CDLL(_libc_path, use_errno=True)
    return _libc


def _syscall(number: int, *args: int) -> int:
    """Raw syscall wrapper."""
    libc = _get_libc()
    libc.syscall.restype = ctypes.c_long
    libc.syscall.argtypes = [ctypes.c_long] + [ctypes.c_long] * len(args)
    ret = libc.syscall(number, *args)
    if ret < 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))
    return ret


# ---------------------------------------------------------------------------
# Struct definitions
# ---------------------------------------------------------------------------

class _LandlockRulesetAttr(ctypes.Structure):
    """struct landlock_ruleset_attr."""
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
    ]


class _LandlockPathBeneathAttr(ctypes.Structure):
    """struct landlock_path_beneath_attr."""
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    ]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_abi_version() -> int:
    """Return the Landlock ABI version supported by the running kernel.

    Returns 0 if Landlock is not available.
    """
    try:
        # landlock_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION)
        # LANDLOCK_CREATE_RULESET_VERSION = 1 << 0 = 1
        version = _syscall(_SYS_LANDLOCK_CREATE_RULESET, 0, 0, 1)
        return version
    except OSError:
        return 0


def is_supported() -> bool:
    """Check if Landlock is supported on this system."""
    if sys.platform != "linux":
        return False
    if _SYS_LANDLOCK_CREATE_RULESET == 0:
        return False
    return get_abi_version() >= 1


def apply_sandbox(
    scratch_dir: str | Path,
    readonly_paths: list[str | Path] | None = None,
) -> bool:
    """Apply Landlock filesystem sandbox to the current process.

    After this call, the process (and all children) can only:
    - Read/write/create/delete files in scratch_dir
    - Read/execute files in readonly_paths

    All other filesystem access is denied by the kernel.

    Args:
        scratch_dir: Full path to the agent's scratch workspace (RW).
        readonly_paths: List of paths for read-only access.
            Defaults to ["/usr", "/nix", "/etc", "/lib", "/lib64", "/bin", "/sbin"].

    Returns:
        True if sandbox was applied successfully.

    Raises:
        OSError: If a syscall fails.
        RuntimeError: If Landlock is not supported.
    """
    if not is_supported():
        raise RuntimeError("Landlock is not supported on this system")

    abi = get_abi_version()
    log.info("Applying Landlock sandbox (ABI v%d)", abi)

    # Determine handled access mask based on ABI version
    handled_access = _ALL_FS_V1
    if abi >= 2:
        handled_access |= LANDLOCK_ACCESS_FS_REFER
    if abi >= 3:
        handled_access |= LANDLOCK_ACCESS_FS_TRUNCATE

    # Adjust RW mask similarly
    rw_access = _READ_WRITE
    if abi >= 3:
        rw_access |= LANDLOCK_ACCESS_FS_TRUNCATE
    ro_access = _READ_ONLY

    if readonly_paths is None:
        readonly_paths = ["/usr", "/nix", "/etc", "/lib", "/lib64", "/bin", "/sbin"]

    # 1. Create ruleset
    attr = _LandlockRulesetAttr(handled_access_fs=handled_access)
    ruleset_fd = _syscall(
        _SYS_LANDLOCK_CREATE_RULESET,
        ctypes.addressof(attr),
        ctypes.sizeof(attr),
        0,
    )
    log.debug("Created Landlock ruleset fd=%d", ruleset_fd)

    try:
        # 2. Add scratch dir (RW)
        scratch = Path(scratch_dir).resolve()
        if scratch.exists():
            _add_path_rule(ruleset_fd, str(scratch), rw_access)
            log.info("Scratch dir (RW): %s", scratch)
        else:
            log.warning("Scratch dir does not exist: %s", scratch)

        # Also grant RW to /tmp for agent temp files
        if Path("/tmp").exists():
            _add_path_rule(ruleset_fd, "/tmp", rw_access)

        # 3. Add read-only paths
        for ro_path in readonly_paths:
            p = Path(ro_path)
            if p.exists():
                _add_path_rule(ruleset_fd, str(p), ro_access)
                log.debug("Read-only path: %s", p)

        # Also add /proc (read-only, needed for Python)
        if Path("/proc").exists():
            _add_path_rule(ruleset_fd, "/proc", ro_access)

        # 4. Set no-new-privs (required before restrict_self)
        libc = _get_libc()
        ret = libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
        if ret != 0:
            raise OSError(ctypes.get_errno(), "prctl(PR_SET_NO_NEW_PRIVS) failed")

        # 5. Restrict self
        _syscall(_SYS_LANDLOCK_RESTRICT_SELF, ruleset_fd, 0)
        log.info("Landlock sandbox applied successfully")
        return True

    finally:
        os.close(ruleset_fd)


def _add_path_rule(ruleset_fd: int, path: str, access: int) -> None:
    """Add a path-beneath rule to the ruleset."""
    fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    try:
        rule = _LandlockPathBeneathAttr(
            allowed_access=access,
            parent_fd=fd,
        )
        _syscall(
            _SYS_LANDLOCK_ADD_RULE,
            ruleset_fd,
            LANDLOCK_RULE_PATH_BENEATH,
            ctypes.addressof(rule),
            0,
        )
    finally:
        os.close(fd)


def classify_path(
    path: str | Path,
    scratch_dir: str | Path,
    readonly_paths: list[str | Path] | None = None,
) -> str:
    """Classify a path as 'rw', 'ro', or 'denied' under the sandbox policy.

    This is a pure-Python check (no kernel involvement) for use in
    pre-screening and UI hints.
    """
    if readonly_paths is None:
        readonly_paths = ["/usr", "/nix", "/etc", "/lib", "/lib64", "/bin", "/sbin"]

    resolved = Path(path).resolve()
    scratch = Path(scratch_dir).resolve()

    # Check scratch (RW)
    try:
        resolved.relative_to(scratch)
        return "rw"
    except ValueError:
        pass

    # Check /tmp
    try:
        resolved.relative_to("/tmp")
        return "rw"
    except ValueError:
        pass

    # Check read-only paths
    for ro in readonly_paths:
        try:
            resolved.relative_to(Path(ro).resolve())
            return "ro"
        except ValueError:
            pass

    # Check /proc
    try:
        resolved.relative_to("/proc")
        return "ro"
    except ValueError:
        pass

    return "denied"
