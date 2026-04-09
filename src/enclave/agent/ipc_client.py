"""IPC client for agent container ↔ orchestrator communication.

Connects to the orchestrator's Unix socket and exchanges messages.
Runs inside the podman container.
"""

from __future__ import annotations

import asyncio
from typing import Callable, Awaitable

from enclave.common.protocol import Message, MessageType


# Type for incoming message handler
IncomingHandler = Callable[[Message], Awaitable[Message | None]]


class IPCClient:
    """Agent-side IPC client that connects to the orchestrator."""

    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._handlers: dict[MessageType, IncomingHandler] = {}
        self._pending: dict[str, asyncio.Future[Message]] = {}
        self._connected = False
        self._listen_task: asyncio.Task | None = None  # type: ignore[type-arg]

    async def connect(self) -> None:
        """Connect to the orchestrator socket."""
        self.reader, self.writer = await asyncio.open_unix_connection(self.socket_path)
        self._connected = True
        self._listen_task = asyncio.create_task(self._listen_loop())

    async def disconnect(self) -> None:
        """Disconnect from the orchestrator."""
        self._connected = False
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
        if self.writer:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except Exception:
                pass

    def on_message(self, msg_type: MessageType, handler: IncomingHandler) -> None:
        """Register a handler for a specific message type from the orchestrator."""
        self._handlers[msg_type] = handler

    async def send(self, msg: Message) -> None:
        """Send a message to the orchestrator (fire and forget)."""
        if not self.writer or not self._connected:
            raise ConnectionError("Not connected to orchestrator")
        data = msg.to_json() + "\n"
        self.writer.write(data.encode())
        await self.writer.drain()

    async def request(self, msg: Message, timeout: float = 30.0) -> Message:
        """Send a message and wait for a reply (matched by reply_to field).

        Args:
            msg: The message to send.
            timeout: Maximum seconds to wait for a reply.

        Returns:
            The response message.

        Raises:
            asyncio.TimeoutError: If no reply within timeout.
        """
        future: asyncio.Future[Message] = asyncio.get_event_loop().create_future()
        self._pending[msg.id] = future
        try:
            await self.send(msg)
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(msg.id, None)

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def _listen_loop(self) -> None:
        """Background task that reads incoming messages."""
        try:
            while self._connected and self.reader:
                line = await self.reader.readline()
                if not line:
                    break

                msg = Message.from_json(line.decode().strip())

                # Check if this is a reply to a pending request
                if msg.reply_to and msg.reply_to in self._pending:
                    self._pending[msg.reply_to].set_result(msg)
                    continue

                # Dispatch handler as a task so the listen loop keeps reading.
                # This prevents deadlock when a handler does ipc.request()
                # (which needs the listen loop to read the reply).
                handler = self._handlers.get(msg.type)
                if handler:
                    asyncio.create_task(self._dispatch(msg, handler))
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        finally:
            self._connected = False

    async def _dispatch(
        self, msg: Message, handler: IncomingHandler,
    ) -> None:
        """Run a handler and send back any response."""
        try:
            response = await handler(msg)
            if response is not None:
                response.reply_to = msg.id
                await self.send(response)
        except Exception as e:
            import sys
            print(f"[ipc] Handler error: {e}", file=sys.stderr)
