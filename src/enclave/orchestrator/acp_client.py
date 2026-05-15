"""ACP (Agent Client Protocol) client for remote Copilot CLI instances.

Implements a JSON-RPC 2.0 client over NDJSON/TCP that speaks the ACP protocol.
Used by ACPBridge to control a remote `copilot --acp --port N` server.

Protocol spec: https://agentclientprotocol.com/protocol/overview
"""

from __future__ import annotations

import asyncio
import json
import ssl
from typing import Any, Callable, Awaitable

from enclave.common.logging import get_logger

log = get_logger("acp-client")

# ACP protocol version we support
ACP_PROTOCOL_VERSION = 1

# Callbacks
UpdateCallback = Callable[[dict], Awaitable[None]]
RequestCallback = Callable[[int | str, str, dict], Awaitable[dict]]


class ACPError(Exception):
    """Error from ACP JSON-RPC response."""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"ACP error {code}: {message}")
        self.code = code
        self.rpc_message = message
        self.data = data


class ACPClient:
    """JSON-RPC client for the Agent Client Protocol over TCP.

    Connects to a remote Copilot CLI running in ACP mode (``copilot --acp --port N``).
    Handles request/response correlation, notification dispatch, and incoming
    server-to-client requests (like ``session/request_permission``).
    """

    def __init__(
        self,
        host: str,
        port: int,
        on_update: UpdateCallback | None = None,
        on_request: RequestCallback | None = None,
        use_tls: bool = False,
        ssl_context: ssl.SSLContext | None = None,
    ):
        self.host = host
        self.port = port
        self._on_update = on_update
        self._on_request = on_request
        self._use_tls = use_tls
        self._ssl_context = ssl_context

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task | None = None
        self._next_id = 1
        self._pending: dict[int | str, asyncio.Future] = {}
        self._connected = False
        self._closing = False

        # Negotiated state
        self.agent_capabilities: dict = {}
        self.agent_info: dict = {}
        self.protocol_version: int = 0

    @property
    def connected(self) -> bool:
        return self._connected and not self._closing

    async def connect(self) -> None:
        """Open TCP connection to the ACP server and start the read loop."""
        if self._connected:
            return

        ssl_ctx = None
        if self._use_tls:
            ssl_ctx = self._ssl_context or ssl.create_default_context()

        log.info("Connecting to ACP server at %s:%d (tls=%s)", self.host, self.port, self._use_tls)
        self._reader, self._writer = await asyncio.open_connection(
            self.host, self.port, ssl=ssl_ctx,
        )
        self._connected = True
        self._closing = False
        self._read_task = asyncio.create_task(self._read_loop())
        log.info("Connected to ACP server at %s:%d", self.host, self.port)

    async def disconnect(self) -> None:
        """Cleanly close the TCP connection."""
        if not self._connected:
            return
        self._closing = True
        self._connected = False

        # Fail all pending requests
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("ACP connection closed"))
        self._pending.clear()

        if self._read_task and not self._read_task.done():
            self._read_task.cancel()
            try:
                await self._read_task
            except (asyncio.CancelledError, Exception):
                pass

        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass

        self._reader = None
        self._writer = None
        log.info("Disconnected from ACP server")

    # -- Public ACP methods --

    async def initialize(self) -> dict:
        """Send ``initialize`` and negotiate capabilities."""
        result = await self._request("initialize", {
            "protocolVersion": ACP_PROTOCOL_VERSION,
            "clientCapabilities": {},
            "clientInfo": {
                "name": "enclave",
                "title": "Enclave Orchestrator",
                "version": "1.0.0",
            },
        })
        self.protocol_version = result.get("protocolVersion", 0)
        self.agent_capabilities = result.get("agentCapabilities", {})
        self.agent_info = result.get("agentInfo", {})
        log.info(
            "ACP initialized: proto=%d agent=%s caps=%s",
            self.protocol_version,
            self.agent_info.get("name", "unknown"),
            list(self.agent_capabilities.keys()),
        )
        return result

    async def new_session(self, cwd: str, mcp_servers: list | None = None) -> str:
        """Create a new ACP session. Returns the session ID."""
        result = await self._request("session/new", {
            "cwd": cwd,
            "mcpServers": mcp_servers or [],
        })
        session_id = result["sessionId"]
        log.info("ACP session created: %s", session_id)
        return session_id

    async def load_session(self, session_id: str, cwd: str) -> None:
        """Load/replay an existing session (history replay via updates)."""
        await self._request("session/load", {
            "sessionId": session_id,
            "cwd": cwd,
            "mcpServers": [],
        })
        log.info("ACP session loaded: %s", session_id)

    async def resume_session(self, session_id: str, cwd: str) -> None:
        """Resume a session without replaying history (if supported)."""
        await self._request("session/resume", {
            "sessionId": session_id,
            "cwd": cwd,
            "mcpServers": [],
        })
        log.info("ACP session resumed: %s", session_id)

    async def prompt(self, session_id: str, text: str) -> dict:
        """Send a user prompt. Blocks until the turn completes.

        While blocked, ``session/update`` notifications fire via the
        ``on_update`` callback for streaming content.
        """
        result = await self._request("session/prompt", {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": text}],
        })
        return result

    async def cancel(self, session_id: str) -> None:
        """Cancel the current prompt turn (notification, no response)."""
        await self._notify("session/cancel", {
            "sessionId": session_id,
        })

    async def set_mode(self, session_id: str, mode: str) -> None:
        """Set agent mode (e.g. 'plan', 'auto') if supported."""
        await self._request("session/set_mode", {
            "sessionId": session_id,
            "mode": mode,
        })

    # -- Permission response --

    async def respond_to_request(self, request_id: int | str, result: dict) -> None:
        """Send a JSON-RPC response to an agent-initiated request.

        Used for responding to ``session/request_permission``.
        """
        msg = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": result,
        }
        await self._send(msg)

    # -- Internals --

    def _next_request_id(self) -> int:
        rid = self._next_id
        self._next_id += 1
        return rid

    async def _request(self, method: str, params: dict) -> dict:
        """Send a JSON-RPC request and await the response."""
        rid = self._next_request_id()
        msg = {
            "jsonrpc": "2.0",
            "id": rid,
            "method": method,
            "params": params,
        }

        future: asyncio.Future[dict] = asyncio.get_event_loop().create_future()
        self._pending[rid] = future

        await self._send(msg)

        try:
            return await future
        except Exception:
            self._pending.pop(rid, None)
            raise

    async def _notify(self, method: str, params: dict) -> None:
        """Send a JSON-RPC notification (no id, no response expected)."""
        msg = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        await self._send(msg)

    async def _send(self, msg: dict) -> None:
        """Write a JSON-RPC message as NDJSON."""
        if not self._writer:
            raise ConnectionError("Not connected to ACP server")
        line = json.dumps(msg, separators=(",", ":")) + "\n"
        self._writer.write(line.encode())
        await self._writer.drain()

    async def _read_loop(self) -> None:
        """Read NDJSON lines from the ACP server and dispatch them."""
        assert self._reader is not None
        try:
            while self._connected:
                line = await self._reader.readline()
                if not line:
                    break

                text = line.decode().strip()
                if not text:
                    continue

                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    log.warning("Invalid JSON from ACP server: %.100s", text)
                    continue

                await self._handle_message(msg)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            if not self._closing:
                log.error("ACP read loop error: %s", e)
        finally:
            if not self._closing:
                self._connected = False
                # Fail pending requests
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(ConnectionError("ACP connection lost"))
                self._pending.clear()
                log.warning("ACP connection lost to %s:%d", self.host, self.port)

    async def _handle_message(self, msg: dict) -> None:
        """Route an incoming JSON-RPC message."""
        if "id" in msg and "result" in msg:
            # Response to our request
            rid = msg["id"]
            fut = self._pending.pop(rid, None)
            if fut and not fut.done():
                fut.set_result(msg["result"])
            return

        if "id" in msg and "error" in msg:
            # Error response to our request
            rid = msg["id"]
            err = msg["error"]
            fut = self._pending.pop(rid, None)
            if fut and not fut.done():
                fut.set_exception(ACPError(
                    err.get("code", -1),
                    err.get("message", "Unknown error"),
                    err.get("data"),
                ))
            return

        if "method" in msg and "id" in msg:
            # Server-to-client request (e.g. session/request_permission)
            method = msg["method"]
            params = msg.get("params", {})
            rid = msg["id"]

            if self._on_request:
                # Dispatch as a task to avoid blocking the read loop
                asyncio.create_task(self._handle_request(rid, method, params))
            else:
                # No handler — send method not found
                error_response = {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "error": {"code": -32601, "message": f"Method not supported: {method}"},
                }
                await self._send(error_response)
            return

        if "method" in msg and "id" not in msg:
            # Notification from server (e.g. session/update)
            method = msg["method"]
            params = msg.get("params", {})

            if method == "session/update" and self._on_update:
                try:
                    await self._on_update(params)
                except Exception as e:
                    log.error("Error in ACP update handler: %s", e)
            else:
                log.debug("Unhandled ACP notification: %s", method)
            return

        log.warning("Unrecognized ACP message: %.200s", json.dumps(msg))

    async def _handle_request(self, rid: int | str, method: str, params: dict) -> None:
        """Handle an incoming server-to-client request in a background task.

        The callback may respond asynchronously (e.g. waiting for user
        permission approval). It returns None to indicate "I will respond
        manually via respond_to_request()", or a dict to auto-respond.
        """
        assert self._on_request is not None
        try:
            result = await self._on_request(rid, method, params)
            if result is not None:
                await self.respond_to_request(rid, result)
        except NotImplementedError:
            error_response = {
                "jsonrpc": "2.0",
                "id": rid,
                "error": {"code": -32601, "message": f"Method not supported: {method}"},
            }
            await self._send(error_response)
        except Exception as e:
            log.error("Error handling ACP request %s: %s", method, e)
            error_response = {
                "jsonrpc": "2.0",
                "id": rid,
                "error": {"code": -32603, "message": str(e)},
            }
            await self._send(error_response)
