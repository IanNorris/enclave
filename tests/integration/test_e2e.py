"""End-to-end integration test: IPC + container + router.

Tests the full flow WITHOUT Matrix (which requires a live homeserver).
Uses the real IPC server and a real podman container running the agent.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest

from enclave.common.config import ContainerConfig
from enclave.common.protocol import Message, MessageType
from enclave.orchestrator.container import ContainerManager
from enclave.orchestrator.ipc import IPCServer


def _podman_available() -> bool:
    """Check if podman is available."""
    import shutil
    return shutil.which("podman") is not None


def _image_exists(image: str = "enclave-agent:latest") -> bool:
    """Check if the agent image exists."""
    import subprocess
    result = subprocess.run(
        ["podman", "image", "exists", image],
        capture_output=True,
    )
    return result.returncode == 0


skip_no_podman = pytest.mark.skipif(
    not _podman_available(), reason="podman not available"
)
skip_no_image = pytest.mark.skipif(
    not _image_exists(), reason="enclave-agent:latest image not built"
)


@skip_no_podman
@skip_no_image
class TestE2EContainerIPC:
    """Test real IPC communication with a podman container."""

    @pytest.mark.asyncio
    async def test_agent_connects_and_reports_ready(self) -> None:
        """Container starts, agent connects via IPC, sends ready status."""
        with tempfile.TemporaryDirectory() as tmpdir:
            socket_dir = Path(tmpdir) / "sockets"
            workspace_dir = Path(tmpdir) / "workspaces"
            session_dir = Path(tmpdir) / "sessions"
            socket_dir.mkdir()
            workspace_dir.mkdir()
            session_dir.mkdir()

            # Create IPC server
            ipc = IPCServer(socket_dir=str(socket_dir))

            received_messages: list[tuple[str, Message]] = []
            ready_event = asyncio.Event()

            async def handler(session_id: str, msg: Message) -> Message | None:
                received_messages.append((session_id, msg))
                if (
                    msg.type == MessageType.STATUS_UPDATE
                    and msg.payload.get("status") == "ready"
                ):
                    ready_event.set()
                return None

            ipc.set_handler(handler)

            session_id = "e2e-test-session"
            socket_path = await ipc.create_socket(session_id)

            # Create container config
            config = ContainerConfig(
                workspace_base=str(workspace_dir),
                session_base=str(session_dir),
            )
            manager = ContainerManager(config=config)

            # Create and start session
            session = await manager.create_session(
                name="E2E Test",
                room_id="!test-room:e2e",
                socket_path=str(socket_path),
            )

            # Override session ID to match our socket
            old_id = session.id
            manager._sessions.pop(old_id)
            session.id = session_id
            session.socket_path = str(socket_path)
            manager._sessions[session_id] = session

            started = await manager.start_session(session_id)
            assert started, "Container failed to start"

            try:
                # Wait for agent to connect and send ready
                await asyncio.wait_for(ready_event.wait(), timeout=30.0)

                # Verify we got a status_update
                assert len(received_messages) >= 1
                sid, msg = received_messages[-1]
                assert sid == session_id
                assert msg.type == MessageType.STATUS_UPDATE
                assert msg.payload["status"] == "ready"
                # SDK availability depends on environment
                assert isinstance(msg.payload["copilot_available"], bool)

            finally:
                await manager.stop_session(session_id)
                await ipc.remove_socket(session_id)

    @pytest.mark.asyncio
    async def test_echo_roundtrip(self) -> None:
        """Send a message to the agent, get an echo response back."""
        with tempfile.TemporaryDirectory() as tmpdir:
            socket_dir = Path(tmpdir) / "sockets"
            workspace_dir = Path(tmpdir) / "workspaces"
            session_dir = Path(tmpdir) / "sessions"
            socket_dir.mkdir()
            workspace_dir.mkdir()
            session_dir.mkdir()

            ipc = IPCServer(socket_dir=str(socket_dir))

            ready_event = asyncio.Event()
            response_event = asyncio.Event()
            response_msg: list[Message] = []

            async def handler(session_id: str, msg: Message) -> Message | None:
                if (
                    msg.type == MessageType.STATUS_UPDATE
                    and msg.payload.get("status") == "ready"
                ):
                    ready_event.set()
                elif msg.type == MessageType.AGENT_RESPONSE:
                    response_msg.append(msg)
                    response_event.set()
                return None

            ipc.set_handler(handler)

            session_id = "e2e-echo-test"
            socket_path = await ipc.create_socket(session_id)

            config = ContainerConfig(
                workspace_base=str(workspace_dir),
                session_base=str(session_dir),
            )
            manager = ContainerManager(config=config)

            session = await manager.create_session(
                name="Echo Test",
                room_id="!echo-room:e2e",
                socket_path=str(socket_path),
            )
            old_id = session.id
            manager._sessions.pop(old_id)
            session.id = session_id
            session.socket_path = str(socket_path)
            manager._sessions[session_id] = session

            started = await manager.start_session(session_id)
            assert started

            try:
                await asyncio.wait_for(ready_event.wait(), timeout=30.0)

                # Send a user message
                await ipc.send_to(
                    session_id,
                    Message(
                        type=MessageType.USER_MESSAGE,
                        payload={
                            "content": "Hello from E2E test!",
                            "sender": "@test:e2e",
                        },
                    ),
                )

                # Wait for echo response
                await asyncio.wait_for(response_event.wait(), timeout=10.0)

                assert len(response_msg) == 1
                assert "[echo] Hello from E2E test!" in response_msg[0].payload["content"]

            finally:
                await manager.stop_session(session_id)
                await ipc.remove_socket(session_id)
