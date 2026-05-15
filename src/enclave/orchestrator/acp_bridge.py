"""ACP Bridge — translates between ACP protocol and Enclave IPC.

The bridge acts as a "virtual agent" on the orchestrator's IPC socket.
It connects to the session's Unix socket (as if it were the agent process)
and translates:

  IPC USER_MESSAGE → ACP session/prompt
  IPC PERMISSION_RESPONSE → ACP permission reply
  IPC SHUTDOWN → ACP session/cancel + disconnect
  ACP session/update(agent_message_chunk) → IPC AGENT_DELTA + AGENT_RESPONSE
  ACP session/update(tool_call) → IPC TOOL_START
  ACP session/update(tool_call_update) → IPC TOOL_COMPLETE
  ACP session/update(plan) → IPC STATUS_UPDATE
  ACP session/request_permission → IPC PERMISSION_REQUEST
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from enclave.common.logging import get_logger
from enclave.common.protocol import Message, MessageType
from enclave.orchestrator.acp_client import ACPClient, ACPError

log = get_logger("acp-bridge")


class ACPBridge:
    """Bridges a remote ACP agent to the Enclave orchestrator via IPC.

    Lifecycle:
        1. Orchestrator creates IPC socket for the session (normal flow).
        2. ACPBridge connects to that socket as the "agent" side.
        3. ACPBridge connects to the remote ACP server.
        4. Messages flow bidirectionally through the bridge.
    """

    def __init__(
        self,
        session_id: str,
        socket_path: str | Path,
        acp_host: str,
        acp_port: int,
        remote_cwd: str = ".",
        acp_session_id: str | None = None,
        use_tls: bool = False,
    ):
        self.session_id = session_id
        self.socket_path = str(socket_path)
        self.remote_cwd = remote_cwd
        self._acp_session_id = acp_session_id
        self._use_tls = use_tls

        # ACP client
        self._acp = ACPClient(
            host=acp_host,
            port=acp_port,
            on_update=self._on_acp_update,
            on_request=self._on_acp_request,
            use_tls=use_tls,
        )

        # IPC connection to orchestrator (we act as the agent)
        self._ipc_reader: asyncio.StreamReader | None = None
        self._ipc_writer: asyncio.StreamWriter | None = None
        self._ipc_read_task: asyncio.Task | None = None

        # State
        self._running = False
        self._prompt_lock = asyncio.Lock()
        self._prompt_task: asyncio.Task | None = None
        self._accumulated_text = ""
        self._tool_states: dict[str, dict] = {}
        self._pending_permissions: dict[str, int | str] = {}
        self._replay_mode = False

    @property
    def acp_session_id(self) -> str | None:
        return self._acp_session_id

    async def start(self) -> None:
        """Connect to both IPC and ACP, initialize the ACP session."""
        log.info("[%s] Starting ACP bridge to %s:%d", self.session_id, self._acp.host, self._acp.port)

        # Connect to the IPC socket (as the agent)
        self._ipc_reader, self._ipc_writer = await asyncio.open_unix_connection(
            self.socket_path
        )
        log.info("[%s] Connected to IPC socket", self.session_id)

        # Connect to remote ACP server
        await self._acp.connect()

        # Initialize ACP
        await self._acp.initialize()

        # Start or resume ACP session
        caps = self._acp.agent_capabilities
        if self._acp_session_id:
            resume_supported = bool(
                caps.get("sessionCapabilities", {}).get("resume")
            )
            if resume_supported:
                try:
                    await self._acp.resume_session(self._acp_session_id, self.remote_cwd)
                    log.info("[%s] Resumed ACP session %s", self.session_id, self._acp_session_id)
                except ACPError:
                    log.warning("[%s] Resume failed, falling back to load", self.session_id)
                    self._replay_mode = True
                    await self._acp.load_session(self._acp_session_id, self.remote_cwd)
                    self._replay_mode = False
            else:
                self._replay_mode = True
                await self._acp.load_session(self._acp_session_id, self.remote_cwd)
                self._replay_mode = False
        else:
            self._acp_session_id = await self._acp.new_session(self.remote_cwd)

        self._running = True

        # Start reading from IPC (orchestrator → bridge)
        self._ipc_read_task = asyncio.create_task(self._ipc_read_loop())

        log.info("[%s] ACP bridge started (acp_session=%s)", self.session_id, self._acp_session_id)

    async def stop(self) -> None:
        """Cleanly shut down the bridge."""
        self._running = False

        # Cancel any active prompt
        if self._prompt_task and not self._prompt_task.done():
            if self._acp.connected and self._acp_session_id:
                try:
                    await self._acp.cancel(self._acp_session_id)
                except Exception:
                    pass
            self._prompt_task.cancel()
            try:
                await self._prompt_task
            except (asyncio.CancelledError, Exception):
                pass

        # Disconnect ACP
        await self._acp.disconnect()

        # Close IPC
        if self._ipc_read_task and not self._ipc_read_task.done():
            self._ipc_read_task.cancel()
            try:
                await self._ipc_read_task
            except (asyncio.CancelledError, Exception):
                pass

        if self._ipc_writer:
            self._ipc_writer.close()
            try:
                await self._ipc_writer.wait_closed()
            except Exception:
                pass

        log.info("[%s] ACP bridge stopped", self.session_id)

    # -- IPC → ACP --

    async def _ipc_read_loop(self) -> None:
        """Read messages from the orchestrator via IPC and translate to ACP."""
        assert self._ipc_reader is not None
        try:
            while self._running:
                line = await self._ipc_reader.readline()
                if not line:
                    break

                try:
                    msg = Message.from_json(line.decode().strip())
                except Exception as e:
                    log.warning("[%s] Bad IPC message: %s", self.session_id, e)
                    continue

                await self._handle_ipc_message(msg)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error("[%s] IPC read loop error: %s", self.session_id, e)

    async def _handle_ipc_message(self, msg: Message) -> None:
        """Handle a message from the orchestrator."""
        if msg.type == MessageType.USER_MESSAGE:
            text = msg.payload.get("content", "")
            if text:
                # Queue the prompt (serialize — only one active at a time)
                asyncio.create_task(self._send_prompt(text))

        elif msg.type == MessageType.PERMISSION_RESPONSE:
            await self._handle_permission_response(msg)

        elif msg.type == MessageType.SHUTDOWN:
            log.info("[%s] Received shutdown, stopping bridge", self.session_id)
            asyncio.create_task(self.stop())

        else:
            log.debug("[%s] Ignoring IPC message type: %s", self.session_id, msg.type.value)

    async def _send_prompt(self, text: str) -> None:
        """Send a prompt to the remote ACP agent (serialized)."""
        async with self._prompt_lock:
            if not self._acp.connected or not self._acp_session_id:
                log.warning("[%s] Cannot send prompt — not connected", self.session_id)
                return

            # Reset per-turn state
            self._accumulated_text = ""
            self._tool_states.clear()

            # Emit synthetic TURN_START
            await self._ipc_send(Message(
                type=MessageType.TURN_START,
                payload={"turn_index": 0},
            ))

            try:
                result = await self._acp.prompt(self._acp_session_id, text)
                stop_reason = result.get("stopReason", "end_turn")

                # Emit final AGENT_RESPONSE with accumulated text
                if self._accumulated_text:
                    await self._ipc_send(Message(
                        type=MessageType.AGENT_RESPONSE,
                        payload={"content": self._accumulated_text},
                    ))

                # Emit synthetic TURN_END
                await self._ipc_send(Message(
                    type=MessageType.TURN_END,
                    payload={"stop_reason": stop_reason},
                ))

            except asyncio.CancelledError:
                await self._ipc_send(Message(
                    type=MessageType.TURN_END,
                    payload={"stop_reason": "cancelled"},
                ))
            except Exception as e:
                log.error("[%s] Prompt error: %s", self.session_id, e)
                await self._ipc_send(Message(
                    type=MessageType.AGENT_RESPONSE,
                    payload={"content": f"[ACP error: {e}]"},
                ))
                await self._ipc_send(Message(
                    type=MessageType.TURN_END,
                    payload={"stop_reason": "error", "error": str(e)},
                ))

    async def _handle_permission_response(self, msg: Message) -> None:
        """Forward an Enclave permission response to ACP."""
        request_id_str = msg.payload.get("request_id", "")
        approved = msg.payload.get("approved", False)

        acp_request_id = self._pending_permissions.pop(request_id_str, None)
        if acp_request_id is None:
            log.warning("[%s] No pending ACP permission for %s", self.session_id, request_id_str)
            return

        if approved:
            outcome = {"outcome": "allowed"}
        else:
            outcome = {"outcome": "rejected"}

        try:
            await self._acp.respond_to_request(acp_request_id, {"outcome": outcome})
        except Exception as e:
            log.error("[%s] Error responding to ACP permission: %s", self.session_id, e)

    # -- ACP → IPC --

    async def _on_acp_update(self, params: dict) -> None:
        """Handle ``session/update`` notifications from the ACP agent."""
        if self._replay_mode:
            return

        update = params.get("update", {})
        update_type = update.get("sessionUpdate", "")

        if update_type == "agent_message_chunk":
            await self._handle_agent_chunk(update)
        elif update_type == "tool_call":
            await self._handle_tool_call(update)
        elif update_type == "tool_call_update":
            await self._handle_tool_call_update(update)
        elif update_type == "plan":
            await self._handle_plan(update)
        elif update_type == "user_message_chunk":
            pass  # Ignore history replay of user messages
        else:
            log.debug("[%s] Unhandled ACP update type: %s", self.session_id, update_type)

    async def _handle_agent_chunk(self, update: dict) -> None:
        """Translate agent_message_chunk → AGENT_DELTA."""
        content = update.get("content", {})
        if content.get("type") == "text":
            chunk_text = content.get("text", "")
            self._accumulated_text += chunk_text

            await self._ipc_send(Message(
                type=MessageType.AGENT_DELTA,
                payload={"content": self._accumulated_text},
            ))

    async def _handle_tool_call(self, update: dict) -> None:
        """Translate tool_call → TOOL_START."""
        tool_call_id = update.get("toolCallId", "")
        title = update.get("title", "")
        kind = update.get("kind", "other")
        status = update.get("status", "pending")

        self._tool_states[tool_call_id] = {
            "title": title,
            "kind": kind,
            "status": status,
        }

        await self._ipc_send(Message(
            type=MessageType.TOOL_START,
            payload={
                "tool_name": kind if kind != "other" else title,
                "detail": title,
                "tool_call_id": tool_call_id,
            },
        ))

    async def _handle_tool_call_update(self, update: dict) -> None:
        """Translate tool_call_update → TOOL_COMPLETE (on terminal status)."""
        tool_call_id = update.get("toolCallId", "")
        status = update.get("status", "")

        state = self._tool_states.get(tool_call_id, {})
        state["status"] = status

        if status in ("completed", "failed"):
            # Extract result content
            content_parts = update.get("content", [])
            result_text = ""
            for part in content_parts:
                if isinstance(part, dict):
                    inner = part.get("content", part)
                    if isinstance(inner, dict) and inner.get("type") == "text":
                        result_text += inner.get("text", "")
                    elif isinstance(inner, str):
                        result_text += inner

            await self._ipc_send(Message(
                type=MessageType.TOOL_COMPLETE,
                payload={
                    "tool_name": state.get("kind", state.get("title", "tool")),
                    "detail": state.get("title", ""),
                    "tool_call_id": tool_call_id,
                    "success": status == "completed",
                    "output": result_text[:2000],
                },
            ))

            self._tool_states.pop(tool_call_id, None)

    async def _handle_plan(self, update: dict) -> None:
        """Translate plan update → STATUS_UPDATE."""
        entries = update.get("entries", [])
        if entries:
            summary = "; ".join(
                e.get("content", "") for e in entries[:5]
            )
            await self._ipc_send(Message(
                type=MessageType.STATUS_UPDATE,
                payload={"status": f"Plan: {summary}"},
            ))

    async def _on_acp_request(self, request_id: int | str, method: str, params: dict) -> dict | None:
        """Handle server-to-client requests from the ACP agent.

        Returns a dict to auto-respond, or None to indicate we'll respond
        manually via acp.respond_to_request().
        """
        if method == "session/request_permission":
            await self._handle_permission_request(request_id, params)
            # Return None — we'll respond when the user grants/denies in WebUI
            return None

        # Unsupported method
        log.warning("[%s] Unsupported ACP client method: %s", self.session_id, method)
        raise NotImplementedError(f"Method not supported: {method}")

    async def _handle_permission_request(self, request_id: int | str, params: dict) -> None:
        """Store the pending permission and forward to orchestrator via IPC."""
        import uuid
        enclave_id = str(uuid.uuid4())
        self._pending_permissions[enclave_id] = request_id

        # Extract permission details from ACP format
        description = params.get("description", "Remote agent requests permission")
        tools = params.get("tools", [])
        tool_names = [t.get("name", "unknown") for t in tools] if tools else []

        await self._ipc_send(Message(
            type=MessageType.PERMISSION_REQUEST,
            payload={
                "request_id": enclave_id,
                "tool": ", ".join(tool_names) if tool_names else "remote_action",
                "description": description,
                "command": params.get("command", ""),
            },
        ))

    # -- IPC helpers --

    async def _ipc_send(self, msg: Message) -> None:
        """Send a message to the orchestrator via IPC."""
        if self._ipc_writer is None:
            return
        try:
            data = msg.to_json() + "\n"
            self._ipc_writer.write(data.encode())
            await self._ipc_writer.drain()
        except Exception as e:
            log.error("[%s] Failed to send IPC message: %s", self.session_id, e)
