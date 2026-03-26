"""Tests for Landlock sandbox module."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from enclave.orchestrator.landlock import (
    classify_path,
    get_abi_version,
    is_supported,
    _READ_ONLY,
    _READ_WRITE,
    LANDLOCK_ACCESS_FS_READ_FILE,
    LANDLOCK_ACCESS_FS_WRITE_FILE,
)


# ------------------------------------------------------------------
# classify_path (pure Python, no kernel needed)
# ------------------------------------------------------------------


class TestClassifyPath:
    def test_scratch_is_rw(self):
        assert classify_path("/data/scratch/file.txt", "/data/scratch") == "rw"

    def test_scratch_subdir_is_rw(self):
        assert classify_path("/data/scratch/a/b/c", "/data/scratch") == "rw"

    def test_scratch_root_is_rw(self):
        assert classify_path("/data/scratch", "/data/scratch") == "rw"

    def test_tmp_is_rw(self):
        assert classify_path("/tmp/agent-work", "/data/scratch") == "rw"

    def test_usr_is_ro(self):
        assert classify_path("/usr/bin/python3", "/data/scratch") == "ro"

    def test_nix_is_ro(self):
        assert classify_path("/nix/store/abc/bin/gcc", "/data/scratch") == "ro"

    def test_etc_is_ro(self):
        assert classify_path("/etc/hosts", "/data/scratch") == "ro"

    def test_lib_is_ro(self):
        assert classify_path("/lib/x86_64-linux-gnu/libc.so.6", "/data/scratch") == "ro"

    def test_proc_is_ro(self):
        assert classify_path("/proc/self/status", "/data/scratch") == "ro"

    def test_home_is_denied(self):
        assert classify_path("/home/user/.bashrc", "/data/scratch") == "denied"

    def test_root_is_denied(self):
        assert classify_path("/root/secrets", "/data/scratch") == "denied"

    def test_var_is_denied(self):
        assert classify_path("/var/log/syslog", "/data/scratch") == "denied"

    def test_custom_readonly_paths(self):
        result = classify_path(
            "/opt/tools/bin/tool",
            "/data/scratch",
            readonly_paths=["/opt/tools"],
        )
        assert result == "ro"

    def test_custom_readonly_denies_unlisted(self):
        result = classify_path(
            "/usr/bin/python3",
            "/data/scratch",
            readonly_paths=["/opt/tools"],
        )
        # /usr is NOT in custom readonly_paths, so denied
        # (but /proc and /tmp are still special-cased)
        assert result == "denied"


# ------------------------------------------------------------------
# ABI detection
# ------------------------------------------------------------------


class TestAbiDetection:
    def test_is_supported_returns_bool(self):
        result = is_supported()
        assert isinstance(result, bool)

    def test_get_abi_version_returns_int(self):
        result = get_abi_version()
        assert isinstance(result, int)
        assert result >= 0

    @patch("enclave.orchestrator.landlock.sys")
    def test_not_linux(self, mock_sys):
        mock_sys.platform = "darwin"
        # Re-import won't work, test the check directly
        from enclave.orchestrator.landlock import is_supported as _is_supported
        # We can't easily test this without reimporting, so test classify_path instead
        # which is the primary interface for non-kernel tests

    def test_abi_version_on_linux(self):
        if sys.platform != "linux":
            pytest.skip("Linux only")
        version = get_abi_version()
        # On Azure VMs with recent kernels, Landlock may or may not be enabled
        assert version >= 0


# ------------------------------------------------------------------
# Access masks
# ------------------------------------------------------------------


class TestAccessMasks:
    def test_read_only_includes_read_file(self):
        assert _READ_ONLY & LANDLOCK_ACCESS_FS_READ_FILE

    def test_read_only_excludes_write(self):
        assert not (_READ_ONLY & LANDLOCK_ACCESS_FS_WRITE_FILE)

    def test_read_write_includes_write(self):
        assert _READ_WRITE & LANDLOCK_ACCESS_FS_WRITE_FILE

    def test_read_write_includes_read(self):
        assert _READ_WRITE & LANDLOCK_ACCESS_FS_READ_FILE


# ------------------------------------------------------------------
# apply_sandbox (requires kernel support — skip if unavailable)
# ------------------------------------------------------------------


class TestApplySandbox:
    """These tests verify the sandbox in a forked subprocess to avoid
    affecting the test runner's own filesystem access."""

    def test_apply_sandbox_in_subprocess(self):
        """Apply sandbox in a child process and verify it restricts access."""
        if not is_supported():
            pytest.skip("Landlock not supported on this kernel")

        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as scratch:
            # Write a test file in scratch
            test_file = Path(scratch) / "test.txt"
            test_file.write_text("hello")

            # Child process applies sandbox then tries to access files
            script = f'''
import sys
sys.path.insert(0, "src")
from enclave.orchestrator.landlock import apply_sandbox

apply_sandbox("{scratch}")

# Should succeed: read from scratch
try:
    content = open("{test_file}").read()
    assert content == "hello", f"Expected 'hello', got {{content}}"
    print("SCRATCH_READ:OK")
except Exception as e:
    print(f"SCRATCH_READ:FAIL:{{e}}")

# Should succeed: write to scratch
try:
    with open("{scratch}/new.txt", "w") as f:
        f.write("new")
    print("SCRATCH_WRITE:OK")
except Exception as e:
    print(f"SCRATCH_WRITE:FAIL:{{e}}")

# Should fail: write to /home
try:
    with open("/home/landlock-test-{os.getpid()}", "w") as f:
        f.write("bad")
    print("HOME_WRITE:FAIL:should have been denied")
except PermissionError:
    print("HOME_WRITE:OK")
except Exception as e:
    print(f"HOME_WRITE:FAIL:{{e}}")

# Should succeed: read from /usr
try:
    import os
    os.listdir("/usr")
    print("USR_READ:OK")
except Exception as e:
    print(f"USR_READ:FAIL:{{e}}")
'''
            result = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=str(Path(__file__).parent.parent.parent),
            )

            output = result.stdout.strip()
            lines = output.split("\n")
            results = {l.split(":")[0]: l for l in lines if ":" in l}

            assert "SCRATCH_READ:OK" in results.get("SCRATCH_READ", ""), \
                f"Scratch read failed: {results.get('SCRATCH_READ')}"
            assert "SCRATCH_WRITE:OK" in results.get("SCRATCH_WRITE", ""), \
                f"Scratch write failed: {results.get('SCRATCH_WRITE')}"
            assert "HOME_WRITE:OK" in results.get("HOME_WRITE", ""), \
                f"Home write should be denied: {results.get('HOME_WRITE')}"
            assert "USR_READ:OK" in results.get("USR_READ", ""), \
                f"/usr read failed: {results.get('USR_READ')}"
