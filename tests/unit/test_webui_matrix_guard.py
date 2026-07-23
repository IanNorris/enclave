"""Tests for the web UI's direct-Matrix write guard (_matrix_write_ok).

The web UI process talks to Matrix directly (not via the orchestrator's
NullMatrixClient), so every direct write must independently respect the
Matrix-disabled setting. The critical case is a session that still carries a
real (pre-disable) Matrix room id: with Matrix off, the web UI must NOT post
to it (that was the observed leak — pasted screenshots showing up in Matrix
after Matrix was disabled).
"""

from __future__ import annotations

from types import SimpleNamespace

from enclave.webui.routes.chat import _matrix_write_ok


def _cfg(enabled: bool):
    return SimpleNamespace(enabled=enabled)


def test_enabled_real_room_allows_write():
    assert _matrix_write_ok(_cfg(True), "!room:server.org") is True


def test_disabled_real_room_blocks_write():
    # Brook's exact case: Matrix disabled but the session kept its real,
    # pre-disable room id. Must NOT write to Matrix.
    assert _matrix_write_ok(_cfg(False), "!room:server.org") is False


def test_enabled_synthetic_room_blocks_write():
    # Matrix-off sessions get synthetic local: room ids — never a real room.
    assert _matrix_write_ok(_cfg(True), "local:abc-123") is False


def test_missing_room_blocks_write():
    assert _matrix_write_ok(_cfg(True), None) is False
    assert _matrix_write_ok(_cfg(True), "") is False


def test_disabled_synthetic_room_blocks_write():
    assert _matrix_write_ok(_cfg(False), "local:abc-123") is False
