"""Client for the Enclave privilege broker.

Communicates with the Rust priv broker daemon via Unix socket.
Used by the orchestrator to request privileged operations.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from enclave.common.logging import get_logger

log = get_logger("priv_client")


class PrivBrokerResult:
    """Result of a privilege broker operation."""

    def __init__(self, data: dict[str, Any]):
        self.id: str = data.get("id", "")
        self.success: bool = data.get("success", False)
        self.exit_code: int | None = data.get("exit_code")
        self.stdout: str = data.get("stdout", "")
        self.stderr: str = data.get("stderr", "")
        self.error: str = data.get("error", "")


class PrivBrokerClient:
    """Async client for the privilege broker daemon."""

    def __init__(self, socket_path: str = "/run/enclave-priv/broker.sock"):
        self.socket_path = socket_path
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> bool:
        """Connect to the broker daemon."""
        try:
            self._reader, self._writer = await asyncio.open_unix_connection(
                self.socket_path
            )
            log.info("Connected to priv broker at %s", self.socket_path)
            return True
        except (FileNotFoundError, ConnectionRefusedError) as e:
            log.error("Failed to connect to priv broker: %s", e)
            return False

    async def disconnect(self) -> None:
        """Disconnect from the broker."""
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
            self._reader = None

    @property
    def is_connected(self) -> bool:
        return self._writer is not None

    async def _send(self, request: dict[str, Any]) -> PrivBrokerResult:
        """Send a request and read the response."""
        if not self._writer or not self._reader:
            return PrivBrokerResult({"error": "Not connected"})

        data = json.dumps(request) + "\n"
        self._writer.write(data.encode())
        await self._writer.drain()

        line = await asyncio.wait_for(self._reader.readline(), timeout=60.0)
        if not line:
            return PrivBrokerResult({"error": "Connection closed"})

        return PrivBrokerResult(json.loads(line.decode().strip()))

    async def ping(self) -> bool:
        """Check if the broker is alive."""
        result = await self._send({
            "type": "ping",
            "id": str(uuid.uuid4()),
        })
        return result.success

    async def exec_command(
        self,
        session_id: str,
        command: str,
        args: list[str] | None = None,
        timeout_secs: int | None = None,
    ) -> PrivBrokerResult:
        """Execute a command as root."""
        return await self._send({
            "type": "exec",
            "id": str(uuid.uuid4()),
            "session_id": session_id,
            "command": command,
            "args": args or [],
            "timeout_secs": timeout_secs,
        })

    async def mount(
        self, session_id: str, source: str, target: str
    ) -> PrivBrokerResult:
        """Bind mount a path."""
        return await self._send({
            "type": "mount",
            "id": str(uuid.uuid4()),
            "session_id": session_id,
            "source": source,
            "target": target,
        })

    async def umount(self, session_id: str, target: str) -> PrivBrokerResult:
        """Unmount a path."""
        return await self._send({
            "type": "umount",
            "id": str(uuid.uuid4()),
            "session_id": session_id,
            "target": target,
        })

    async def make_shared(self, session_id: str, path: str) -> PrivBrokerResult:
        """Set up shared mount propagation."""
        return await self._send({
            "type": "make_shared",
            "id": str(uuid.uuid4()),
            "session_id": session_id,
            "path": path,
        })
