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


async def handle_user_message(
    ipc: IPCClient,
    sdk_session: _CopilotSession | None,
    msg: Message,
    loop: asyncio.AbstractEventLoop,
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

    try:
        from copilot.generated.session_events import SessionEventType
    except ImportError:
        await ipc.send(Message(
            type=MessageType.AGENT_RESPONSE,
            payload={"content": f"[echo] {content}", "in_reply_to": msg.id},
            reply_to=msg.id,
        ))
        return

    idle_event = asyncio.Event()
    accumulated_content = []
    error_msg: str | None = None

    def _fire_and_forget(coro: object) -> None:
        """Schedule an async send from a sync callback."""
        future = asyncio.run_coroutine_threadsafe(coro, loop)  # type: ignore[arg-type]
        future.add_done_callback(
            lambda f: f.exception() if not f.cancelled() else None
        )

    def on_event(event: object) -> None:
        nonlocal error_msg
        etype = getattr(event, "type", None)
        data = getattr(event, "data", None)

        if etype == SessionEventType.ASSISTANT_MESSAGE_DELTA:
            delta = getattr(data, "delta_content", None) or getattr(data, "content", None) or ""
            if delta:
                accumulated_content.append(delta)
                full_text = "".join(accumulated_content)
                _fire_and_forget(ipc.send(Message(
                    type=MessageType.AGENT_DELTA,
                    payload={"content": full_text, "in_reply_to": msg.id},
                    reply_to=msg.id,
                )))

        elif etype == SessionEventType.ASSISTANT_MESSAGE:
            final = getattr(data, "content", None) or ""
            if final:
                accumulated_content.clear()
                accumulated_content.append(final)

        elif etype == SessionEventType.ASSISTANT_INTENT:
            intent = getattr(data, "intent", None) or ""
            if intent:
                _fire_and_forget(ipc.send(Message(
                    type=MessageType.AGENT_THINKING,
                    payload={"intent": intent, "in_reply_to": msg.id},
                    reply_to=msg.id,
                )))

        elif etype == SessionEventType.TOOL_EXECUTION_START:
            tool_name = getattr(data, "tool_name", None) or getattr(data, "name", None) or "unknown"
            _fire_and_forget(ipc.send(Message(
                type=MessageType.TOOL_START,
                payload={"tool_name": tool_name, "in_reply_to": msg.id},
                reply_to=msg.id,
            )))

        elif etype == SessionEventType.TOOL_EXECUTION_COMPLETE:
            tool_name = getattr(data, "tool_name", None) or getattr(data, "name", None) or "unknown"
            _fire_and_forget(ipc.send(Message(
                type=MessageType.TOOL_COMPLETE,
                payload={"tool_name": tool_name, "in_reply_to": msg.id},
                reply_to=msg.id,
            )))

        elif etype == SessionEventType.SESSION_IDLE:
            idle_event.set()

        elif etype == SessionEventType.SESSION_ERROR:
            error_msg = getattr(data, "message", str(data))
            idle_event.set()

    unsubscribe = sdk_session.on(on_event)
    try:
        await sdk_session.send(content)
        await asyncio.wait_for(idle_event.wait(), timeout=600.0)
    except TimeoutError:
        error_msg = "Agent timed out after 600s."
    finally:
        unsubscribe()

    # Send final response
    final_content = "".join(accumulated_content) if accumulated_content else "[no response]"
    if error_msg:
        final_content = f"[error] {error_msg}"

    await ipc.send(Message(
        type=MessageType.AGENT_RESPONSE,
        payload={"content": final_content, "in_reply_to": msg.id},
        reply_to=msg.id,
    ))


async def try_init_copilot(
    working_directory: str = "/workspace",
) -> tuple[_CopilotClient, _CopilotSession] | None:
    """Try to initialize the Copilot SDK.

    Returns (client, session) tuple or None if SDK unavailable.
    """
    try:
        from copilot import (
            CopilotClient,
            PermissionRequestResult,
            SubprocessConfig,
            SystemMessageAppendConfig,
        )
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

        session = await client.create_session(
            on_permission_request=lambda _req, _meta: PermissionRequestResult(
                kind="approved"
            ),
            system_message=SystemMessageAppendConfig(
                append=(
                    "You are an AI assistant running inside an Enclave sandbox. "
                    "You can help the user with coding, research, and system tasks. "
                    "File operations are limited to the /workspace directory."
                )
            ),
            working_directory=working_directory,
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
    sdk_result = await try_init_copilot()
    if sdk_result:
        sdk_client, sdk_session = sdk_result
        print("[agent] Copilot SDK initialized")
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
        await handle_user_message(ipc, sdk_session, msg, loop)
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
