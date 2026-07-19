"""Tests for making Matrix optional (web-UI-only mode).

Covers the config derivation of ``matrix.enabled``, the synthetic room-id
helpers, and the ``NullMatrixClient`` no-op surface (including the drift guard
that it covers every method the orchestrator actually calls).
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from enclave.common.config import (
    MatrixConfig,
    is_synthetic_room,
    make_synthetic_room_id,
    load_config,
)
from enclave.orchestrator.null_matrix_client import NullMatrixClient


# ── Config derivation ──

class TestMatrixEnabledDerivation:
    def test_creds_present_enables(self) -> None:
        m = MatrixConfig(homeserver="h", user_id="u", password="p")
        assert m.enabled is True

    def test_no_creds_disables(self) -> None:
        assert MatrixConfig().enabled is False

    def test_explicit_true_wins_over_missing_creds(self) -> None:
        # Operator explicitly asked for Matrix — honour it (startup then
        # hard-fails on the missing creds, by design).
        assert MatrixConfig(enabled=True).enabled is True

    def test_explicit_false_wins_over_creds(self) -> None:
        m = MatrixConfig(homeserver="h", user_id="u", password="p", enabled=False)
        assert m.enabled is False

    def test_yaml_omitted_no_creds_disables(self, tmp_path: Path) -> None:
        p = tmp_path / "c.yaml"
        p.write_text("matrix:\n  homeserver: ''\n")
        assert load_config(p).matrix.enabled is False

    def test_yaml_explicit_false_with_creds(self, tmp_path: Path) -> None:
        p = tmp_path / "c.yaml"
        p.write_text(
            "matrix:\n"
            "  enabled: false\n"
            "  homeserver: 'https://h'\n"
            "  user_id: '@u:h'\n"
            "  password: 'pw'\n"
        )
        assert load_config(p).matrix.enabled is False

    def test_env_override_off_beats_creds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        p = tmp_path / "c.yaml"
        p.write_text(
            "matrix:\n"
            "  homeserver: 'https://h'\n"
            "  user_id: '@u:h'\n"
            "  password: 'pw'\n"
        )
        monkeypatch.setenv("ENCLAVE_MATRIX_ENABLED", "false")
        assert load_config(p).matrix.enabled is False

    def test_env_override_parses_false_string(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Guard the bool("false") is True trap.
        monkeypatch.setenv("ENCLAVE_MATRIX_ENABLED", "false")
        assert load_config().matrix.enabled is False


# ── Synthetic room ids ──

class TestSyntheticRoom:
    def test_make_is_recognised(self) -> None:
        r = make_synthetic_room_id()
        assert is_synthetic_room(r)
        assert r.startswith("local:")

    def test_real_room_not_synthetic(self) -> None:
        assert not is_synthetic_room("!abc:server.org")

    def test_empty_and_none_not_synthetic(self) -> None:
        assert not is_synthetic_room("")
        assert not is_synthetic_room(None)

    def test_ids_unique(self) -> None:
        assert make_synthetic_room_id() != make_synthetic_room_id()


# ── Null client ──

# Every method the orchestrator invokes on self.matrix (see grep of
# self.matrix.<method> across router.py / session_manager.py, plus _trust_users).
_CALLED_METHODS = {
    "on_message", "on_user_join", "on_reaction", "on_poll_response",
    "login", "initial_sync", "sync_forever", "stop_sync", "close",
    "send_message", "send_reaction", "edit_message", "redact_event",
    "set_typing", "send_poll", "end_poll", "get_event_count",
    "reset_event_count", "purge_room_history", "create_room", "create_space",
    "join_room", "leave_room", "forget_room", "invite_user", "kick_user",
    "cleanup_room", "_trust_users", "download_media", "upload_file",
}


class TestNullMatrixClient:
    def test_covers_all_called_methods(self) -> None:
        missing = {m for m in _CALLED_METHODS if not hasattr(NullMatrixClient, m)}
        assert not missing, f"NullMatrixClient missing: {missing}"

    def test_disabled_flag(self) -> None:
        assert NullMatrixClient.enabled is False
        n = NullMatrixClient()
        assert n.client.rooms == {}
        assert n.client.logged_in is False

    def test_create_room_returns_none(self) -> None:
        # Fail-fast, not fake-success: never fabricate a room id.
        assert asyncio.run(NullMatrixClient().create_room(name="x")) is None

    def test_poll_and_media_are_honest(self) -> None:
        n = NullMatrixClient()
        assert asyncio.run(n.send_poll("r", "q", [])) is None
        assert asyncio.run(n.upload_file("r", "/f")) is None
        assert asyncio.run(n.download_media("mxc://x/y", "/tmp/z")) is False

    def test_membership_noops_return_false(self) -> None:
        n = NullMatrixClient()
        assert asyncio.run(n.join_room("r")) is False
        assert asyncio.run(n.invite_user("r", "@u:h")) is False

    def test_sync_forever_returns_immediately(self) -> None:
        # Must not block the loop forever even if scheduled.
        asyncio.run(asyncio.wait_for(NullMatrixClient().sync_forever(), timeout=1.0))

    def test_login_reports_success(self) -> None:
        assert asyncio.run(NullMatrixClient().login()) is True


class TestNullClientDriftGuard:
    """Introspective guard: the null client must expose every public method the
    real client does, so it can't silently drift when a method is added.

    Skipped if matrix-nio is not installed (the null client itself never needs
    it, but importing the real client for comparison does).
    """

    def test_null_covers_real_public_surface(self) -> None:
        pytest.importorskip("nio")
        from enclave.orchestrator.matrix_client import EnclaveMatrixClient

        def public(cls):
            return {
                n for n, _ in inspect.getmembers(cls, predicate=inspect.isfunction)
                if not n.startswith("__")
                and not (n.startswith("_") and n != "_trust_users")
            }

        real = public(EnclaveMatrixClient)
        null = public(NullMatrixClient)
        missing = real - null
        assert not missing, f"NullMatrixClient missing real methods: {missing}"

