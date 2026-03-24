"""Agent entry point — runs inside the podman container.

Connects to the orchestrator via IPC socket, initializes the Copilot SDK,
and routes messages between the orchestrator and the AI model.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import TYPE_CHECKING

from enclave.agent.ipc_client import IPCClient
from enclave.common.protocol import Message, MessageType

if TYPE_CHECKING:
    from copilot import CopilotClient as _CopilotClient
    from copilot.session import CopilotSession as _CopilotSession


def setup_session_listener(
    ipc: IPCClient,
    sdk_session: _CopilotSession,
    loop: asyncio.AbstractEventLoop,
) -> callable:
    """Register a persistent event listener on the SDK session.

    Returns unsubscribe callable.  Events are forwarded to the orchestrator
    via IPC for the lifetime of the session — this handles background agents
    that produce additional turns after the initial SESSION_IDLE.
    """
    from copilot.generated.session_events import SessionEventType

    # Track the "current" user message so replies are correlated.
    current_msg_id: str | None = None
    accumulated_content: list[str] = []

    def _fire_and_forget(coro: object) -> None:
        future = asyncio.run_coroutine_threadsafe(coro, loop)  # type: ignore[arg-type]
        future.add_done_callback(
            lambda f: f.exception() if not f.cancelled() else None
        )

    def on_event(event: object) -> None:
        etype = getattr(event, "type", None)
        data = getattr(event, "data", None)
        reply_to = current_msg_id

        if etype == SessionEventType.ASSISTANT_MESSAGE_DELTA:
            delta = getattr(data, "delta_content", None) or getattr(data, "content", None) or ""
            if delta:
                accumulated_content.append(delta)
                full_text = "".join(accumulated_content)
                _fire_and_forget(ipc.send(Message(
                    type=MessageType.AGENT_DELTA,
                    payload={"content": full_text, "in_reply_to": reply_to},
                    reply_to=reply_to,
                )))

        elif etype == SessionEventType.ASSISTANT_MESSAGE:
            # Complete message from the assistant — send as AGENT_RESPONSE.
            final = getattr(data, "content", None) or ""
            if final:
                accumulated_content.clear()
                accumulated_content.append(final)
                _fire_and_forget(ipc.send(Message(
                    type=MessageType.AGENT_RESPONSE,
                    payload={"content": final, "in_reply_to": reply_to},
                    reply_to=reply_to,
                )))

        elif etype == SessionEventType.ASSISTANT_INTENT:
            intent = getattr(data, "intent", None) or ""
            if intent:
                _fire_and_forget(ipc.send(Message(
                    type=MessageType.AGENT_THINKING,
                    payload={"intent": intent, "in_reply_to": reply_to},
                    reply_to=reply_to,
                )))

        elif etype == SessionEventType.TOOL_EXECUTION_START:
            tool_name = getattr(data, "tool_name", None) or getattr(data, "name", None) or "unknown"
            args = getattr(data, "arguments", None) or {}
            if isinstance(args, str):
                try:
                    import json as _json
                    args = _json.loads(args)
                except Exception:
                    args = {}
            description = args.get("description", "") or args.get("intent", "") or args.get("prompt", "")
            _fire_and_forget(ipc.send(Message(
                type=MessageType.TOOL_START,
                payload={
                    "tool_name": tool_name,
                    "description": str(description)[:200],
                    "tool_call_id": getattr(data, "tool_call_id", None) or getattr(data, "toolCallId", "") or "",
                    "in_reply_to": reply_to,
                },
                reply_to=reply_to,
            )))

        elif etype == SessionEventType.TOOL_EXECUTION_COMPLETE:
            tool_name = getattr(data, "tool_name", None) or getattr(data, "name", None) or "unknown"
            success = getattr(data, "success", True)
            result = getattr(data, "result", None)
            result_preview = ""
            if result:
                result_preview = getattr(result, "content", "") or str(result)
            _fire_and_forget(ipc.send(Message(
                type=MessageType.TOOL_COMPLETE,
                payload={
                    "tool_name": tool_name,
                    "success": success,
                    "result_preview": str(result_preview)[:200],
                    "tool_call_id": getattr(data, "tool_call_id", None) or getattr(data, "toolCallId", "") or "",
                    "in_reply_to": reply_to,
                },
                reply_to=reply_to,
            )))

        elif etype == SessionEventType.SUBAGENT_STARTED:
            agent_name = getattr(data, "name", None) or getattr(data, "agent_name", "") or "sub-agent"
            description = getattr(data, "description", "") or ""
            _fire_and_forget(ipc.send(Message(
                type=MessageType.SUBAGENT_STARTED,
                payload={
                    "agent_name": str(agent_name),
                    "description": str(description)[:200],
                    "in_reply_to": reply_to,
                },
                reply_to=reply_to,
            )))

        elif etype in (SessionEventType.SUBAGENT_COMPLETED, SessionEventType.SUBAGENT_FAILED):
            agent_name = getattr(data, "name", None) or getattr(data, "agent_name", "") or "sub-agent"
            _fire_and_forget(ipc.send(Message(
                type=MessageType.SUBAGENT_COMPLETED,
                payload={
                    "agent_name": str(agent_name),
                    "success": etype == SessionEventType.SUBAGENT_COMPLETED,
                    "in_reply_to": reply_to,
                },
                reply_to=reply_to,
            )))

        elif etype == SessionEventType.ASSISTANT_TURN_START:
            turn_id = getattr(data, "turn_id", None) or getattr(data, "turnId", "")
            _fire_and_forget(ipc.send(Message(
                type=MessageType.TURN_START,
                payload={"turn_id": str(turn_id), "in_reply_to": reply_to},
                reply_to=reply_to,
            )))

        elif etype == SessionEventType.ASSISTANT_TURN_END:
            turn_id = getattr(data, "turn_id", None) or getattr(data, "turnId", "")
            _fire_and_forget(ipc.send(Message(
                type=MessageType.TURN_END,
                payload={"turn_id": str(turn_id), "in_reply_to": reply_to},
                reply_to=reply_to,
            )))

        elif etype == SessionEventType.SESSION_ERROR:
            err = getattr(data, "message", str(data))
            _fire_and_forget(ipc.send(Message(
                type=MessageType.AGENT_RESPONSE,
                payload={"content": f"[error] {err}", "in_reply_to": reply_to},
                reply_to=reply_to,
            )))

    def set_current_msg(msg_id: str | None) -> None:
        nonlocal current_msg_id
        current_msg_id = msg_id
        accumulated_content.clear()

    unsubscribe = sdk_session.on(on_event)
    # Attach the helper so callers can update the current msg reference.
    unsubscribe.set_current_msg = set_current_msg  # type: ignore[attr-defined]
    return unsubscribe


async def handle_user_message(
    ipc: IPCClient,
    sdk_session: _CopilotSession | None,
    msg: Message,
    loop: asyncio.AbstractEventLoop,
    listener_ctl: object | None = None,
) -> None:
    """Handle a user message — stream events back via IPC."""
    content = msg.payload.get("content", "")

    if sdk_session is None:
        await ipc.send(Message(
            type=MessageType.AGENT_RESPONSE,
            payload={"content": f"[echo] {content}", "in_reply_to": msg.id},
            reply_to=msg.id,
        ))
        return

    # Point the persistent listener at this message
    if listener_ctl and hasattr(listener_ctl, "set_current_msg"):
        listener_ctl.set_current_msg(msg.id)

    try:
        await sdk_session.send(content)
        # Don't wait for SESSION_IDLE here — the persistent listener handles
        # all responses including those from background sub-agents.
    except Exception as e:
        await ipc.send(Message(
            type=MessageType.AGENT_RESPONSE,
            payload={"content": f"[error] {e}", "in_reply_to": msg.id},
            reply_to=msg.id,
        ))


async def try_init_copilot(
    working_directory: str = "/workspace",
    ipc: IPCClient | None = None,
) -> tuple[_CopilotClient, _CopilotSession] | None:
    """Try to initialize the Copilot SDK.

    Attempts to resume the most recent session first (preserving conversation
    history across container restarts). Falls back to creating a new session.

    Returns (client, session) tuple or None if SDK unavailable.
    """
    try:
        from copilot import (
            CopilotClient,
            PermissionRequestResult,
            SubprocessConfig,
            SystemMessageAppendConfig,
        )
        from copilot.types import Tool, ToolResult
    except ImportError:
        return None

    try:
        github_token = os.environ.get("GITHUB_TOKEN")
        sdk_config = SubprocessConfig(github_token=github_token) if github_token else None

        client = CopilotClient(sdk_config)
        await client.start()

        # Verify authentication before creating a session
        try:
            auth = await client.get_auth_status()
            if not auth.isAuthenticated:
                print("[agent] Copilot SDK: not authenticated, falling back to echo", file=sys.stderr)
                await client.stop()
                return None
        except Exception as e:
            print(f"[agent] Copilot SDK auth check failed: {e}", file=sys.stderr)
            await client.stop()
            return None

        perm_handler = lambda _req, _meta: PermissionRequestResult(kind="approved")
        sys_msg = SystemMessageAppendConfig(
            append=(
                "You are an AI assistant running inside an Enclave sandbox. "
                "You can help the user with coding, research, and system tasks. "
                "File operations are limited to the /workspace directory.\n\n"
                "IMPORTANT: When you create images or files that the user should see, "
                "use the `send_file` tool to send them to the chat. The `view` tool "
                "only lets YOU see the file — the user cannot see it unless you send it.\n\n"
                "PRIVILEGE ESCALATION: You have a `sudo` tool that executes commands as root "
                "on the HOST system. The user must approve each request via Matrix reactions. "
                "Use it for package installation (apt), service management (systemctl), "
                "system configuration, etc. Always provide a clear 'reason' so the user "
                "knows why root is needed. You have internet access via slirp4netns networking."
            )
        )

        # Custom tool: send_file — sends a file to the user via Matrix
        async def _send_file_handler(invocation: object) -> ToolResult:
            args = getattr(invocation, "arguments", {}) or {}
            file_path = args.get("path", "")
            caption = args.get("caption", "")
            if not file_path:
                return ToolResult(
                    text_result_for_llm="Error: 'path' parameter is required",
                    result_type="error",
                )
            if not os.path.isfile(file_path):
                return ToolResult(
                    text_result_for_llm=f"Error: file not found: {file_path}",
                    result_type="error",
                )
            if ipc and ipc.is_connected:
                await ipc.send(Message(
                    type=MessageType.FILE_SEND,
                    payload={"file_path": file_path, "body": caption},
                ))
                return ToolResult(
                    text_result_for_llm=f"File sent to chat: {file_path}",
                )
            return ToolResult(
                text_result_for_llm="Error: not connected to orchestrator",
                result_type="error",
            )

        send_file_tool = Tool(
            name="send_file",
            description=(
                "Send a file (image, document, etc.) to the user in the chat room. "
                "Use this after creating images, screenshots, or any file the user should see. "
                "The 'view' tool only shows files to YOU — use send_file to show them to the USER."
            ),
            handler=_send_file_handler,
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path to the file to send",
                    },
                    "caption": {
                        "type": "string",
                        "description": "Optional caption/description for the file",
                    },
                },
                "required": ["path"],
            },
            skip_permission=True,
        )

        custom_tools = [send_file_tool]

        # Custom tool: sudo — request privileged command execution
        async def _sudo_handler(invocation: object) -> ToolResult:
            args = getattr(invocation, "arguments", {}) or {}
            command = args.get("command", "")
            cmd_args = args.get("args", [])
            reason = args.get("reason", "")
            if not command:
                return ToolResult(
                    text_result_for_llm="Error: 'command' parameter is required",
                    result_type="error",
                )
            if not ipc or not ipc.is_connected:
                return ToolResult(
                    text_result_for_llm="Error: not connected to orchestrator",
                    result_type="error",
                )
            try:
                response = await ipc.request(
                    Message(
                        type=MessageType.PRIVILEGE_REQUEST,
                        payload={
                            "command": command,
                            "args": cmd_args,
                            "reason": reason,
                        },
                    ),
                    timeout=360.0,  # 6 min — user needs time to react
                )
                payload = response.payload
                if not payload.get("approved"):
                    return ToolResult(
                        text_result_for_llm=(
                            f"Privilege request denied: {payload.get('error', 'unknown')}"
                        ),
                        result_type="error",
                    )
                exit_code = payload.get("exit_code", -1)
                stdout = payload.get("stdout", "")
                stderr = payload.get("stderr", "")
                error = payload.get("error", "")
                parts = []
                if stdout:
                    parts.append(f"stdout:\n{stdout}")
                if stderr:
                    parts.append(f"stderr:\n{stderr}")
                if error:
                    parts.append(f"error: {error}")
                result_text = "\n".join(parts) or "(no output)"
                return ToolResult(
                    text_result_for_llm=(
                        f"Command exited with code {exit_code}\n{result_text}"
                    ),
                )
            except asyncio.TimeoutError:
                return ToolResult(
                    text_result_for_llm="Privilege request timed out (no approval received)",
                    result_type="error",
                )

        sudo_tool = Tool(
            name="sudo",
            description=(
                "Execute a command with root privileges on the HOST system. "
                "The user will be prompted to approve the request via Matrix reactions. "
                "Use this for system administration tasks like installing packages, "
                "managing services, editing system config files, or anything requiring root. "
                "Example: sudo(command='apt', args=['install', '-y', 'nginx'], "
                "reason='User asked to install nginx')."
            ),
            handler=_sudo_handler,
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The command to execute (e.g., 'apt', 'systemctl')",
                    },
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Command arguments (e.g., ['install', '-y', 'nginx'])",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why this privileged command is needed (shown to user for approval)",
                    },
                },
                "required": ["command", "reason"],
            },
            skip_permission=True,
        )
        custom_tools.append(sudo_tool)

        # Try to resume the most recent session (preserves conversation history)
        try:
            last_id = await client.get_last_session_id()
            if last_id:
                print(f"[agent] Resuming session {last_id}", file=sys.stderr)
                session = await client.resume_session(
                    last_id,
                    on_permission_request=perm_handler,
                    system_message=sys_msg,
                    working_directory=working_directory,
                    tools=custom_tools,
                )
                print(f"[agent] Session resumed: {last_id}", file=sys.stderr)
                return (client, session)
        except Exception as e:
            print(f"[agent] Session resume failed ({e}), creating new session", file=sys.stderr)

        # No previous session or resume failed — create fresh
        session = await client.create_session(
            on_permission_request=perm_handler,
            system_message=sys_msg,
            working_directory=working_directory,
            tools=custom_tools,
        )
        return (client, session)
    except Exception as e:
        print(f"[agent] Copilot SDK init failed: {e}", file=sys.stderr)
        return None


async def main() -> None:
    """Agent main loop."""
    socket_path = os.environ.get("IPC_SOCKET", "/socket/orchestrator.sock")
    session_id = os.environ.get("SESSION_ID", "unknown")
    session_name = os.environ.get("SESSION_NAME", "unknown")

    print(f"[agent] Starting agent: {session_name} ({session_id})")
    print(f"[agent] Socket: {socket_path}")

    loop = asyncio.get_running_loop()

    # Connect to orchestrator
    ipc = IPCClient(socket_path)

    retries = 0
    while retries < 10:
        try:
            await ipc.connect()
            break
        except (FileNotFoundError, ConnectionRefusedError):
            retries += 1
            print(f"[agent] Waiting for socket... ({retries}/10)")
            await asyncio.sleep(1)
    else:
        print("[agent] Failed to connect to orchestrator", file=sys.stderr)
        sys.exit(1)

    print("[agent] Connected to orchestrator")

    # Try to init Copilot SDK
    sdk_result = await try_init_copilot(ipc=ipc)
    listener_ctl = None
    if sdk_result:
        sdk_client, sdk_session = sdk_result
        print("[agent] Copilot SDK initialized")
        # Register persistent event listener (handles background agents too)
        try:
            listener_ctl = setup_session_listener(ipc, sdk_session, loop)
            print("[agent] Persistent event listener registered")
        except ImportError:
            print("[agent] SessionEventType not available, running in echo mode")
            sdk_client, sdk_session = None, None
    else:
        sdk_client, sdk_session = None, None
        print("[agent] Running in echo mode (no Copilot SDK)")

    # Send ready status
    await ipc.send(Message(
        type=MessageType.STATUS_UPDATE,
        payload={
            "status": "ready",
            "session_id": session_id,
            "copilot_available": sdk_session is not None,
        },
    ))

    # Register message handlers
    async def on_user_message(msg: Message) -> Message | None:
        await handle_user_message(ipc, sdk_session, msg, loop, listener_ctl)
        return None

    async def on_shutdown(msg: Message) -> Message | None:
        print("[agent] Shutdown requested")
        await ipc.disconnect()
        return None

    ipc.on_message(MessageType.USER_MESSAGE, on_user_message)
    ipc.on_message(MessageType.SHUTDOWN, on_shutdown)

    print("[agent] Ready and listening")

    # Keep alive until disconnected
    try:
        while ipc.is_connected:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass

    # Cleanup
    if listener_ctl and callable(listener_ctl):
        try:
            listener_ctl()
        except Exception:
            pass
    if sdk_session:
        try:
            sdk_session.disconnect()
        except Exception:
            pass
    if sdk_client:
        try:
            await sdk_client.stop()
        except Exception:
            pass

    await ipc.disconnect()
    print("[agent] Shut down")


if __name__ == "__main__":
    asyncio.run(main())
