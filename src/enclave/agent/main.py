"""Agent entry point — runs inside the podman container.

Connects to the orchestrator via IPC socket, initializes the Copilot SDK,
and routes messages between the orchestrator and the AI model.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
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
        def _on_done(f: object) -> None:
            try:
                exc = f.exception() if not f.cancelled() else None  # type: ignore[union-attr]
                if exc:
                    print(f"[agent] IPC send error: {exc}", file=sys.stderr)
            except Exception:
                pass
        future.add_done_callback(_on_done)

    def on_event(event: object) -> None:
        etype = getattr(event, "type", None)
        data = getattr(event, "data", None)
        reply_to = current_msg_id

        # Log all events for diagnostics
        etype_str = getattr(etype, "value", str(etype)) if etype else "None"
        print(f"[agent] Event: {etype_str}", file=sys.stderr)

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

        elif etype == SessionEventType.ASSISTANT_REASONING_DELTA:
            delta = getattr(data, "delta_content", None) or ""
            if delta:
                _fire_and_forget(ipc.send(Message(
                    type=MessageType.AGENT_THINKING,
                    payload={
                        "reasoning_delta": delta,
                        "reasoning_id": getattr(data, "reasoning_id", None) or "",
                        "in_reply_to": reply_to,
                    },
                    reply_to=reply_to,
                )))

        elif etype == SessionEventType.ASSISTANT_REASONING:
            text = getattr(data, "reasoning_text", None) or ""
            if text:
                _fire_and_forget(ipc.send(Message(
                    type=MessageType.AGENT_THINKING,
                    payload={
                        "reasoning": text,
                        "reasoning_id": getattr(data, "reasoning_id", None) or "",
                        "in_reply_to": reply_to,
                    },
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
            print(f"[agent] SESSION_ERROR: {err}", file=sys.stderr)
            _fire_and_forget(ipc.send(Message(
                type=MessageType.AGENT_RESPONSE,
                payload={"content": f"[error] {err}", "in_reply_to": reply_to},
                reply_to=reply_to,
            )))

        elif etype == SessionEventType.SESSION_COMPACTION_START:
            print("[agent] Compaction started", file=sys.stderr)
            _fire_and_forget(ipc.send(Message(
                type=MessageType.STATUS_UPDATE,
                payload={"status": "compacting", "detail": "Context compaction in progress"},
                reply_to=reply_to,
            )))

        elif etype == SessionEventType.SESSION_COMPACTION_COMPLETE:
            msgs_removed = getattr(data, "messages_removed", None)
            tokens_removed = getattr(data, "tokens_removed", None)
            pre_tokens = getattr(data, "pre_compaction_tokens", None)
            post_tokens = getattr(data, "post_compaction_tokens", None)
            print(
                f"[agent] Compaction complete: {msgs_removed} msgs removed, "
                f"{tokens_removed} tokens removed, "
                f"{pre_tokens} → {post_tokens} tokens",
                file=sys.stderr,
            )
            _fire_and_forget(ipc.send(Message(
                type=MessageType.STATUS_UPDATE,
                payload={
                    "status": "compaction_complete",
                    "messages_removed": msgs_removed,
                    "tokens_removed": tokens_removed,
                    "pre_compaction_tokens": pre_tokens,
                    "post_compaction_tokens": post_tokens,
                },
                reply_to=reply_to,
            )))

        elif etype == SessionEventType.SESSION_TRUNCATION:
            print(f"[agent] Session truncation: {data}", file=sys.stderr)

        else:
            # Log unhandled events for diagnostics
            if etype_str not in ("assistant.usage", "session.idle"):
                print(f"[agent] Unhandled event: {etype_str}", file=sys.stderr)

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
        print(f"[agent] Sending to SDK: {content[:100]}...", file=sys.stderr)
        await sdk_session.send(content)
        print(f"[agent] SDK send() returned", file=sys.stderr)
        # Don't wait for SESSION_IDLE here — the persistent listener handles
        # all responses including those from background sub-agents.
    except Exception as e:
        print(f"[agent] SDK send() error: {e}", file=sys.stderr)
        await ipc.send(Message(
            type=MessageType.AGENT_RESPONSE,
            payload={"content": f"[error] {e}", "in_reply_to": msg.id},
            reply_to=msg.id,
        ))


# ---------------------------------------------------------------------------
# Host-mode permission screening helpers
# ---------------------------------------------------------------------------

# System tools that require approval when running on the host (non-YOLO).
_RESTRICTED_COMMANDS = {
    # System package managers
    "apt", "apt-get", "dpkg", "pacman", "dnf", "yum", "zypper", "brew",
    "snap", "flatpak", "nix-env",
    # Global package installs
    "pip", "pip3", "npm", "yarn", "pnpm", "gem", "cargo", "go",
    # Service management
    "systemctl", "service", "journalctl",
    # System modification
    "mount", "umount", "fdisk", "mkfs", "modprobe",
    "useradd", "usermod", "groupadd", "chown", "chmod",
    # Dangerous
    "dd", "rm",
}


def _is_restricted_command(cmd_text: str) -> bool:
    """Check if a shell command invokes a restricted system tool."""
    import shlex
    # Handle pipes/chains — check each segment
    for segment in cmd_text.replace("&&", ";").replace("||", ";").split(";"):
        segment = segment.strip()
        if segment.startswith("|"):
            segment = segment.lstrip("| ")
        try:
            tokens = shlex.split(segment)
        except ValueError:
            tokens = segment.split()
        if not tokens:
            continue
        # Skip env vars (KEY=val cmd ...) and sudo/env wrappers
        cmd = tokens[0]
        for t in tokens:
            if "=" not in t:
                cmd = t
                break
        base = os.path.basename(cmd)
        if base in _RESTRICTED_COMMANDS:
            return True
        # pip install --user is fine; global pip install is not
        # but we screen pip anyway — user can approve
    return False


def _is_in_scratch(path: str, scratch: str) -> bool:
    """Return True if *path* is inside the scratch (working) directory."""
    if not path:
        return True  # empty path = relative = in scratch
    try:
        resolved = os.path.realpath(path)
        scratch_resolved = os.path.realpath(scratch)
        return resolved == scratch_resolved or resolved.startswith(scratch_resolved + os.sep)
    except (OSError, ValueError):
        return False


def _request_permission_sync(
    ipc: "IPCClient | None",
    perm_type: str,
    target: str,
    reason: str,
) -> "PermissionRequestResult":
    """Send a permission request to the orchestrator and wait for approval.

    The SDK supports async permission handlers (returning Awaitable), so
    this actually returns a coroutine despite the name.  Kept as a regular
    function that returns an awaitable for compatibility.
    """
    from copilot import PermissionRequestResult

    if not ipc or not ipc.is_connected:
        return PermissionRequestResult(
            kind="denied-by-rules",
            message="Cannot reach orchestrator for approval",
        )

    async def _ask() -> PermissionRequestResult:
        try:
            response = await ipc.request(
                Message(
                    type=MessageType.PERMISSION_REQUEST,
                    payload={
                        "perm_type": perm_type,
                        "target": target,
                        "reason": reason,
                    },
                ),
                timeout=360.0,
            )
            if response.payload.get("approved", False):
                return PermissionRequestResult(kind="approved")
            return PermissionRequestResult(
                kind="denied-interactively-by-user",
                message=f"User denied access to: {target}",
            )
        except Exception as exc:
            print(f"[agent] Permission request failed: {exc}", file=sys.stderr)
            return PermissionRequestResult(
                kind="denied-by-rules",
                message=f"Permission request failed: {exc}",
            )

    return _ask()  # Returns a coroutine (Awaitable) — SDK will await it


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

        # Persist SDK state (sessions, history) to the workspace so it
        # survives container restarts.
        state_dir = os.path.join(working_directory, ".copilot-state")
        os.makedirs(state_dir, exist_ok=True)

        cli_args = ["--config-dir", state_dir]
        sdk_config = SubprocessConfig(
            github_token=github_token,
            cli_args=cli_args,
        ) if github_token else SubprocessConfig(cli_args=cli_args)

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

        # Build profile-aware system prompt from external files
        profile_name = os.environ.get("ENCLAVE_PROFILE", "dev")
        is_host = profile_name == "host"
        is_yolo = os.environ.get("ENCLAVE_YOLO") == "1"

        # Permission handler: screens SDK tool requests for host profile
        def perm_handler(_req: object, _meta: object) -> PermissionRequestResult:
            # Containers are already sandboxed — auto-approve everything
            if not is_host:
                return PermissionRequestResult(kind="approved")

            # YOLO mode: auto-approve all SDK tools (sudo still goes through
            # its own IPC approval flow since it's a custom tool)
            if is_yolo:
                return PermissionRequestResult(kind="approved")

            # Host mode (non-YOLO): screen for restricted operations
            kind = getattr(_req, "kind", "")

            if kind == "shell":
                cmd_text = getattr(_req, "full_command_text", "") or ""
                # Check if the command uses restricted system tools
                if _is_restricted_command(cmd_text):
                    reason = getattr(_req, "intention", "") or f"Run: {cmd_text[:100]}"
                    return _request_permission_sync(
                        ipc, "command", cmd_text, reason,
                    )
                # Check if the command touches paths outside the scratch space
                paths = getattr(_req, "possible_paths", []) or []
                outside = [p for p in paths if not _is_in_scratch(p, working_directory)]
                if outside:
                    reason = getattr(_req, "intention", "") or f"Access: {', '.join(outside[:3])}"
                    return _request_permission_sync(
                        ipc, "filesystem", outside[0], reason,
                    )
                return PermissionRequestResult(kind="approved")

            if kind == "read":
                path = getattr(_req, "path", "") or ""
                if not _is_in_scratch(path, working_directory):
                    reason = getattr(_req, "intention", "") or f"Read: {path}"
                    return _request_permission_sync(
                        ipc, "filesystem", path, reason,
                    )
                return PermissionRequestResult(kind="approved")

            if kind == "write":
                path = getattr(_req, "file_name", "") or ""
                if not _is_in_scratch(path, working_directory):
                    reason = getattr(_req, "intention", "") or f"Write: {path}"
                    return _request_permission_sync(
                        ipc, "filesystem", path, reason,
                    )
                return PermissionRequestResult(kind="approved")

            # url, mcp, memory, hook, custom-tool — auto-approve
            return PermissionRequestResult(kind="approved")

        prompt_dir = Path(__file__).parent / "prompts"
        prompt_parts = []
        for filename in ("base.md", f"{profile_name}.md"):
            prompt_file = prompt_dir / filename
            if prompt_file.exists():
                prompt_parts.append(prompt_file.read_text())
            else:
                print(f"[agent] Warning: prompt file not found: {prompt_file}", file=sys.stderr)

        sys_msg = SystemMessageAppendConfig(
            append="\n\n".join(prompt_parts)
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
            suggested_pattern = args.get("suggested_pattern", "")
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
                payload = {
                    "command": command,
                    "args": cmd_args,
                    "reason": reason,
                }
                if suggested_pattern:
                    payload["suggested_pattern"] = suggested_pattern
                response = await ipc.request(
                    Message(
                        type=MessageType.PRIVILEGE_REQUEST,
                        payload=payload,
                    ),
                    timeout=360.0,  # 6 min — user needs time to vote
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
                "Execute a command as root on the HOST system. The user approves via a poll. "
                "Use ONLY for operations that need root: package installation (apt), "
                "service management (systemctl), editing system config files, etc. "
                "Do NOT use sudo to run regular programs — host binaries are mounted "
                "read-only in your container at /host/usr/ and are in your PATH. "
                "After `sudo apt install figlet`, just run `figlet Hello` directly. "
                "Suggest a regex pattern for repeated command categories. "
                "Example: sudo(command='apt-get', args=['install', '-y', 'nginx'], "
                "reason='Install nginx', suggested_pattern='^apt-get\\s+')."
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
                    "suggested_pattern": {
                        "type": "string",
                        "description": "Optional regex pattern to suggest for blanket approval (e.g., '^apt\\s+' for all apt commands)",
                    },
                },
                "required": ["command", "reason"],
            },
            skip_permission=True,
        )
        custom_tools.append(sudo_tool)

        # Custom tool: request_mount — request a host path be mounted into container
        async def _mount_handler(invocation: object) -> ToolResult:
            args = getattr(invocation, "arguments", {}) or {}
            source_path = args.get("source_path", "")
            reason = args.get("reason", "")
            suggested_pattern = args.get("suggested_pattern", "")
            if not source_path:
                return ToolResult(
                    text_result_for_llm="Error: 'source_path' parameter is required",
                    result_type="error",
                )
            if not ipc or not ipc.is_connected:
                return ToolResult(
                    text_result_for_llm="Error: not connected to orchestrator",
                    result_type="error",
                )
            try:
                payload: dict[str, Any] = {
                    "source_path": source_path,
                    "reason": reason,
                }
                if suggested_pattern:
                    payload["suggested_pattern"] = suggested_pattern
                response = await ipc.request(
                    Message(
                        type=MessageType.MOUNT_REQUEST,
                        payload=payload,
                    ),
                    timeout=360.0,
                )
                rpayload = response.payload
                if not rpayload.get("approved"):
                    return ToolResult(
                        text_result_for_llm=(
                            f"Mount request denied: {rpayload.get('error', 'unknown')}"
                        ),
                        result_type="error",
                    )
                error = rpayload.get("error")
                if error:
                    return ToolResult(
                        text_result_for_llm=f"Mount failed: {error}",
                        result_type="error",
                    )
                container_path = rpayload.get("container_path", "")
                return ToolResult(
                    text_result_for_llm=(
                        f"Mounted {source_path} at {container_path}\n"
                        f"You can now access files at {container_path}"
                    ),
                )
            except asyncio.TimeoutError:
                return ToolResult(
                    text_result_for_llm="Mount request timed out (no approval received)",
                    result_type="error",
                )

        mount_tool = Tool(
            name="request_mount",
            description=(
                "Request a host directory be mounted into your container. "
                "The user must approve via a poll. Once approved, the path appears "
                "at /workspace/<mount-name> and is accessible immediately. "
                "Use for: accessing project directories, data files, config dirs, etc. "
                "Example: request_mount(source_path='/home/ian/projects/myapp', "
                "reason='Access project source code')."
            ),
            handler=_mount_handler,
            parameters={
                "type": "object",
                "properties": {
                    "source_path": {
                        "type": "string",
                        "description": "Absolute path on the host to mount (e.g., '/home/ian/projects/myapp')",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why this mount is needed (shown to user for approval)",
                    },
                    "suggested_pattern": {
                        "type": "string",
                        "description": "Optional regex pattern for blanket approval (e.g., '^mount:/home/ian/projects/')",
                    },
                },
                "required": ["source_path", "reason"],
            },
            skip_permission=True,
        )
        custom_tools.append(mount_tool)

        # Try to resume the most recent session (preserves conversation history)
        infinite_sessions_config = {
            "enabled": True,
            "background_compaction_threshold": 0.8,
            "buffer_exhaustion_threshold": 0.95,
        }

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
                    infinite_sessions=infinite_sessions_config,
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
            infinite_sessions=infinite_sessions_config,
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
