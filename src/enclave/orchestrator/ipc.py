"""IPC server for orchestrator ↔ agent container communication.

Manages Unix socket connections to agent containers. Each container
gets its own socket. Protocol is newline-delimited JSON.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Callable, Awaitable

from enclave.common.logging import get_logger
from enclave.common.protocol import Message, MessageType

log = get_logger("ipc")

# Type for message handler callbacks
MessageHandler = Callable[[str, Message], Awaitable[Message | None]]


class IPCConnection:
    """A single IPC connection to an agent container."""

    def __init__(
        self,
        session_id: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ):
        self.session_id = session_id
        self.reader = reader
        self.writer = writer
        self._closed = False

    async def send(self, msg: Message) -> None:
        """Send a message to the agent."""
        if self._closed:
            log.warning("Attempted send on closed connection: %s", self.session_id)
            return
        data = msg.to_json() + "\n"
        self.writer.write(data.encode())
        await self.writer.drain()

    async def recv(self, timeout: float = 30.0) -> Message | None:
        """Receive a message from the agent."""
        try:
            line = await asyncio.wait_for(self.reader.readline(), timeout=timeout)
            if not line:
                return None
            return Message.from_json(line.decode().strip())
        except asyncio.TimeoutError:
            return None
        except Exception as e:
            log.error("Error receiving from %s: %s", self.session_id, e)
            return None

    async def close(self) -> None:
        """Close the connection."""
        if not self._closed:
            self._closed = True
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except Exception:
                pass

    @property
    def is_closed(self) -> bool:
        return self._closed


class IPCServer:
    """Manages IPC sockets for agent containers.

    Each agent session gets a dedicated Unix socket. The server listens
    on that socket and routes messages to registered handlers.
    """

    def __init__(self, socket_dir: str | Path):
        self.socket_dir = Path(socket_dir)
        self.socket_dir.mkdir(parents=True, exist_ok=True)
        self._servers: dict[str, asyncio.Server] = {}
        self._connections: dict[str, IPCConnection] = {}
        self._handler: MessageHandler | None = None
        self._connect_callbacks: list[Callable[[str], Awaitable[None]]] = []
        self._disconnect_callbacks: list[Callable[[str], Awaitable[None]]] = []

    def set_handler(self, handler: MessageHandler) -> None:
        """Set the message handler for incoming agent messages.

        Handler receives (session_id, message) and optionally returns a response.
        """
        self._handler = handler

    def on_connect(self, callback: Callable[[str], Awaitable[None]]) -> None:
        """Register a callback for when an agent connects."""
        self._connect_callbacks.append(callback)

    def on_disconnect(self, callback: Callable[[str], Awaitable[None]]) -> None:
        """Register a callback for when an agent disconnects."""
        self._disconnect_callbacks.append(callback)

    def socket_path(self, session_id: str) -> Path:
        """Get the socket path for a session."""
        return self.socket_dir / f"{session_id}.sock"

    async def create_socket(self, session_id: str) -> Path:
        """Create a listening socket for a session.

        Returns the socket path (to be bind-mounted into the container).
        """
        path = self.socket_path(session_id)
        if path.exists():
            path.unlink()

        server = await asyncio.start_unix_server(
            lambda r, w: self._handle_connection(session_id, r, w),
            path=str(path),
        )
        os.chmod(str(path), 0o660)

        self._servers[session_id] = server
        log.info("IPC socket created: %s", path)
        return path

    async def remove_socket(self, session_id: str) -> None:
        """Remove a session's socket and close connections."""
        if session_id in self._connections:
            conn = self._connections[session_id]
            await conn.close()
            del self._connections[session_id]

        if session_id in self._servers:
            self._servers[session_id].close()
            await self._servers[session_id].wait_closed()
            del self._servers[session_id]

        path = self.socket_path(session_id)
        if path.exists():
            path.unlink()

        log.info("IPC socket removed: %s", session_id)

    async def send_to(self, session_id: str, msg: Message) -> bool:
        """Send a message to a specific agent.

        Returns True if sent, False if agent not connected.
        """
        conn = self._connections.get(session_id)
        if conn is None or conn.is_closed:
            log.warning("No connection for session %s", session_id)
            return False
        await conn.send(msg)
        return True

    def is_connected(self, session_id: str) -> bool:
        """Check if an agent is connected."""
        conn = self._connections.get(session_id)
        return conn is not None and not conn.is_closed

    def connected_sessions(self) -> list[str]:
        """List all connected session IDs."""
        return [
            sid for sid, conn in self._connections.items()
            if not conn.is_closed
        ]

    async def close_all(self) -> None:
        """Close all sockets and connections."""
        for session_id in list(self._servers.keys()):
            await self.remove_socket(session_id)
        log.info("All IPC sockets closed")

    async def _handle_connection(
        self,
        session_id: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a new agent connection."""
        conn = IPCConnection(session_id, reader, writer)
        self._connections[session_id] = conn
        log.info("Agent connected: %s", session_id)

        for cb in self._connect_callbacks:
            try:
                await cb(session_id)
            except Exception as e:
                log.error("Connect callback error: %s", e)

        try:
            while not conn.is_closed:
                msg = await conn.recv(timeout=None)  # Block until message
                if msg is None:
                    break

                log.debug("Received from %s: %s", session_id, msg.type.value)

                if self._handler:
                    try:
                        response = await self._handler(session_id, msg)
                        if response is not None:
                            await conn.send(response)
                    except Exception as e:
                        log.error("Handler error for %s: %s", session_id, e)
                        error_msg = Message(
                            type=MessageType.SHUTDOWN,
                            payload={"error": str(e)},
                            reply_to=msg.id,
                        )
                        await conn.send(error_msg)
        except Exception as e:
            log.error("Connection error for %s: %s", session_id, e)
        finally:
            await conn.close()
            if session_id in self._connections:
                del self._connections[session_id]

            log.info("Agent disconnected: %s", session_id)
            for cb in self._disconnect_callbacks:
                try:
                    await cb(session_id)
                except Exception as e:
                    log.error("Disconnect callback error: %s", e)
