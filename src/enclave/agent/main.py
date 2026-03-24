"""Agent entry point — runs inside the podman container.

Connects to the orchestrator via IPC socket, initializes the Copilot SDK,
and routes messages between the orchestrator and the AI model.
"""

from __future__ import annotations

import asyncio
import os
import sys

from enclave.agent.ipc_client import IPCClient
from enclave.common.protocol import Message, MessageType


async def handle_user_message(
    client: IPCClient,
    sdk_session: object | None,
    msg: Message,
) -> None:
    """Handle a user message forwarded from the orchestrator.

    Routes it through the Copilot SDK and sends the response back.
    """
    content = msg.payload.get("content", "")
    sender = msg.payload.get("sender", "unknown")

    if sdk_session is None:
        # SDK not available — echo mode for testing
        response_text = f"[echo] {content}"
    else:
        # Route through Copilot SDK
        try:
            from copilot_sdk import SessionEvent

            event: SessionEvent = await sdk_session.send_and_wait(content, timeout=120.0)
            response_text = event.data.content if event and event.data else "[no response]"
        except Exception as e:
            response_text = f"[error] {e}"

    await client.send(Message(
        type=MessageType.AGENT_RESPONSE,
        payload={
            "content": response_text,
            "in_reply_to": msg.id,
        },
        reply_to=msg.id,
    ))


async def try_init_copilot() -> tuple[object, object] | None:
    """Try to initialize the Copilot SDK.

    Returns (client, session) tuple or None if SDK unavailable.
    """
    try:
        from copilot_sdk import CopilotClient, PermissionRequestResult
        from copilot_sdk.types import SystemMessageAppendConfig

        client = CopilotClient()
        await client.start()

        async def permission_handler(request: object) -> PermissionRequestResult:
            return PermissionRequestResult(kind="approved")

        session = await client.create_session(
            on_permission_request=permission_handler,
            system_message=SystemMessageAppendConfig(
                append=(
                    "You are an AI assistant running inside an Enclave sandbox. "
                    "You can help the user with coding, research, and system tasks. "
                    "File operations are limited to the /workspace directory."
                )
            ),
        )
        return (client, session)
    except ImportError:
        return None
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
        await handle_user_message(ipc, sdk_session, msg)
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
            await sdk_session.disconnect()
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
