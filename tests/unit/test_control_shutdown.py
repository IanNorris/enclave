"""The control server must shut down promptly even with live subscribers.

A web UI keeps long-lived ``subscribe`` / ``subscribe_notifications``
connections open. Those handlers block in a ``while True`` loop for the life
of the connection, so ``Server.wait_closed()`` used to hang orchestrator
shutdown until each connection's per-iteration timeout elapsed (~90s system
shutdown delay). ``stop()`` now cancels in-flight client handlers and bounds
``wait_closed()``, so it must return quickly.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from enclave.orchestrator.control import ControlServer


def _server(tmp_path: Path) -> ControlServer:
    router = SimpleNamespace(
        sessions=SimpleNamespace(get_session=lambda _sid: None),
        containers=SimpleNamespace(list_sessions=lambda: []),
    )
    return ControlServer(tmp_path / "control.sock", router)


@pytest.mark.asyncio
async def test_stop_is_prompt_with_active_subscriber(tmp_path: Path) -> None:
    srv = _server(tmp_path)
    await srv.start()

    # Open a long-lived notification subscription, like the web UI does.
    reader, writer = await asyncio.open_unix_connection(str(tmp_path / "control.sock"))
    writer.write(b'{"action":"subscribe_notifications"}\n')
    await writer.drain()
    # Wait for the subscription ack so the handler is parked in its loop.
    await asyncio.wait_for(reader.readline(), timeout=5.0)
    # Let the server-side handler task register itself.
    await asyncio.sleep(0.05)
    assert srv._client_tasks, "handler task should be tracked"

    # stop() must not wait for the subscriber's 600s per-iteration timeout.
    await asyncio.wait_for(srv.stop(), timeout=5.0)

    assert not (tmp_path / "control.sock").exists()

    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass


@pytest.mark.asyncio
async def test_stop_without_clients_is_clean(tmp_path: Path) -> None:
    srv = _server(tmp_path)
    await srv.start()
    await asyncio.wait_for(srv.stop(), timeout=5.0)
    assert not (tmp_path / "control.sock").exists()
