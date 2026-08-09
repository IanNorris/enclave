"""Tests for bounded Copilot SDK authentication startup retries."""

import asyncio
from types import SimpleNamespace

import pytest

from enclave.agent import main as agent_main
from enclave.agent.main import (
    _authenticate_started_client,
    _wait_for_copilot_auth,
)


class _AuthClient:
    def __init__(self, outcomes: list[bool | Exception]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    async def get_auth_status(self) -> SimpleNamespace:
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(isAuthenticated=outcome)


class _StartedClient:
    def __init__(self, stop_error: Exception | None = None) -> None:
        self.stopped = False
        self.stop_error = stop_error

    async def stop(self) -> None:
        self.stopped = True
        if self.stop_error:
            raise self.stop_error


@pytest.mark.asyncio
async def test_auth_retry_recovers_from_transient_false() -> None:
    client = _AuthClient([False, False, True])

    result = await _wait_for_copilot_auth(client, retry_delays=(0, 0))

    assert result is True
    assert client.calls == 3


@pytest.mark.asyncio
async def test_auth_retry_recovers_from_transient_error() -> None:
    client = _AuthClient([RuntimeError("SDK not ready"), True])

    result = await _wait_for_copilot_auth(client, retry_delays=(0,))

    assert result is True
    assert client.calls == 2


@pytest.mark.asyncio
async def test_auth_retry_exhausts_before_echo_fallback() -> None:
    client = _AuthClient([False, False, False])

    result = await _wait_for_copilot_auth(client, retry_delays=(0, 0))

    assert result is False
    assert client.calls == 3


@pytest.mark.asyncio
async def test_auth_ready_immediately_does_not_sleep() -> None:
    client = _AuthClient([True])

    result = await _wait_for_copilot_auth(client, retry_delays=(99,))

    assert result is True
    assert client.calls == 1


@pytest.mark.asyncio
async def test_cancelled_auth_startup_stops_unpublished_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _StartedClient()

    async def _cancel(_: object) -> bool:
        raise asyncio.CancelledError

    monkeypatch.setattr(agent_main, "_wait_for_copilot_auth", _cancel)

    with pytest.raises(asyncio.CancelledError):
        await _authenticate_started_client(client)

    assert client.stopped is True


@pytest.mark.asyncio
async def test_cancelled_auth_preserves_cancellation_if_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _StartedClient(RuntimeError("stop failed"))

    async def _cancel(_: object) -> bool:
        raise asyncio.CancelledError

    monkeypatch.setattr(agent_main, "_wait_for_copilot_auth", _cancel)

    with pytest.raises(asyncio.CancelledError):
        await _authenticate_started_client(client)

    assert client.stopped is True
