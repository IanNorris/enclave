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
    timestamp = msg.payload.get("timestamp", "")

    # Prepend current time context so the agent knows when the message was sent
    if timestamp:
        content = f"<current_datetime>{timestamp}</current_datetime>\n\n{content}"

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
        print(f"[agent] Profile: {profile_name} (host={is_host}, yolo={is_yolo})", file=sys.stderr)

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
                text = prompt_file.read_text()
                prompt_parts.append(text)
                print(f"[agent] Loaded prompt: {filename} ({len(text)} bytes)", file=sys.stderr)
            else:
                print(f"[agent] Warning: prompt file not found: {prompt_file}", file=sys.stderr)

        # Inject user identity into prompt if available
        user_name = os.environ.get("ENCLAVE_USER_NAME", "")
        user_pronouns = os.environ.get("ENCLAVE_USER_PRONOUNS", "")
        if user_name:
            identity_line = f"The user's name is **{user_name}**"
            if user_pronouns:
                identity_line += f" ({user_pronouns})"
            identity_line += ". Address them by name."
            prompt_parts.insert(0, identity_line)
            print(f"[agent] User: {user_name} ({user_pronouns})", file=sys.stderr)

        # Load key memories from workspace (written by orchestrator)
        memories_path = Path(working_directory) / ".enclave-memories"
        if memories_path.exists():
            memories_text = memories_path.read_text().strip()
            if memories_text:
                prompt_parts.append(memories_text)
                print(f"[agent] Loaded key memories ({len(memories_text)} chars)", file=sys.stderr)

        sys_msg = SystemMessageAppendConfig(
            content="\n\n".join(prompt_parts)
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

        # Custom tool: schedule_cron — register a recurring callback
        async def _schedule_handler(invocation: object) -> ToolResult:
            args = getattr(invocation, "arguments", {}) or {}
            interval_hours = args.get("interval_hours", 0)
            reason = args.get("reason", "")
            schedule_id = args.get("id", "")
            if not reason:
                return ToolResult(
                    text_result_for_llm="Error: 'reason' parameter is required",
                    result_type="error",
                )
            if not ipc or not ipc.is_connected:
                return ToolResult(
                    text_result_for_llm="Error: not connected to orchestrator",
                    result_type="error",
                )
            try:
                payload: dict[str, Any] = {
                    "interval_seconds": int(interval_hours * 3600),
                    "reason": reason,
                }
                if schedule_id:
                    payload["id"] = schedule_id
                response = await ipc.request(
                    Message(type=MessageType.SCHEDULE_SET, payload=payload),
                    timeout=10.0,
                )
                rp = response.payload
                if rp.get("error"):
                    return ToolResult(
                        text_result_for_llm=f"Schedule error: {rp['error']}",
                        result_type="error",
                    )
                return ToolResult(
                    text_result_for_llm=(
                        f"Schedule registered: {rp.get('id')}\n"
                        f"Next fire: {rp.get('next_fire')}"
                    ),
                )
            except asyncio.TimeoutError:
                return ToolResult(
                    text_result_for_llm="Schedule request timed out",
                    result_type="error",
                )

        schedule_tool = Tool(
            name="schedule_cron",
            description=(
                "Register a recurring callback. You will receive a message at each interval. "
                "Minimum interval is 1 hour. Use for periodic checks, monitoring, or reminders. "
                "Example: schedule_cron(interval_hours=2, reason='Check build status')"
            ),
            handler=_schedule_handler,
            parameters={
                "type": "object",
                "properties": {
                    "interval_hours": {
                        "type": "number",
                        "description": "Interval between callbacks in hours (minimum 1)",
                    },
                    "reason": {
                        "type": "string",
                        "description": "What to do when the schedule fires (you'll see this as a message)",
                    },
                    "id": {
                        "type": "string",
                        "description": "Optional ID for the schedule (for cancellation)",
                    },
                },
                "required": ["interval_hours", "reason"],
            },
            skip_permission=True,
        )
        custom_tools.append(schedule_tool)

        # Custom tool: set_timer — one-shot wake-up
        async def _timer_handler(invocation: object) -> ToolResult:
            args = getattr(invocation, "arguments", {}) or {}
            delay_hours = args.get("delay_hours", 0)
            at_time = args.get("at_time", "")
            reason = args.get("reason", "")
            timer_id = args.get("id", "")
            if not reason:
                return ToolResult(
                    text_result_for_llm="Error: 'reason' parameter is required",
                    result_type="error",
                )
            if not delay_hours and not at_time:
                return ToolResult(
                    text_result_for_llm="Error: provide either 'delay_hours' or 'at_time'",
                    result_type="error",
                )
            if not ipc or not ipc.is_connected:
                return ToolResult(
                    text_result_for_llm="Error: not connected to orchestrator",
                    result_type="error",
                )
            try:
                payload: dict[str, Any] = {"reason": reason}
                if timer_id:
                    payload["id"] = timer_id
                if delay_hours:
                    payload["delay_seconds"] = int(delay_hours * 3600)
                elif at_time:
                    # Parse ISO 8601 timestamp
                    from datetime import datetime as _dt, timezone as _tz
                    try:
                        dt = _dt.fromisoformat(at_time)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=_tz.utc)
                        payload["fire_at"] = dt.timestamp()
                    except ValueError:
                        return ToolResult(
                            text_result_for_llm=f"Invalid time format: {at_time}. Use ISO 8601.",
                            result_type="error",
                        )
                response = await ipc.request(
                    Message(type=MessageType.TIMER_SET, payload=payload),
                    timeout=10.0,
                )
                rp = response.payload
                if rp.get("error"):
                    return ToolResult(
                        text_result_for_llm=f"Timer error: {rp['error']}",
                        result_type="error",
                    )
                return ToolResult(
                    text_result_for_llm=(
                        f"Timer set: {rp.get('id')}\n"
                        f"Will fire at: {rp.get('fire_at')}"
                    ),
                )
            except asyncio.TimeoutError:
                return ToolResult(
                    text_result_for_llm="Timer request timed out",
                    result_type="error",
                )

        timer_tool = Tool(
            name="set_timer",
            description=(
                "Set a one-shot timer to wake you up. The container will be restarted if needed. "
                "Specify either delay_hours (relative) or at_time (absolute ISO 8601 UTC). "
                "Example: set_timer(delay_hours=1, reason='Check if deploy completed') "
                "Example: set_timer(at_time='2025-01-15T14:00:00Z', reason='Send daily summary')"
            ),
            handler=_timer_handler,
            parameters={
                "type": "object",
                "properties": {
                    "delay_hours": {
                        "type": "number",
                        "description": "Hours from now to fire (e.g., 1.5 for 90 minutes)",
                    },
                    "at_time": {
                        "type": "string",
                        "description": "Absolute UTC time in ISO 8601 format",
                    },
                    "reason": {
                        "type": "string",
                        "description": "What to do when the timer fires (you'll see this as a message)",
                    },
                    "id": {
                        "type": "string",
                        "description": "Optional ID for the timer (for cancellation)",
                    },
                },
                "required": ["reason"],
            },
            skip_permission=True,
        )
        custom_tools.append(timer_tool)

        # Custom tool: launch_gui — launch a GUI app on the user's desktop
        async def _gui_handler(invocation: object) -> ToolResult:
            args = getattr(invocation, "arguments", {}) or {}
            command = args.get("command", "")
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
                        type=MessageType.GUI_LAUNCH_REQUEST,
                        payload={"command": command, "reason": reason},
                    ),
                    timeout=360.0,
                )
                rp = response.payload
                if rp.get("error"):
                    return ToolResult(
                        text_result_for_llm=f"GUI launch failed: {rp['error']}",
                        result_type="error",
                    )
                return ToolResult(
                    text_result_for_llm=f"GUI app launched: {command}",
                )
            except asyncio.TimeoutError:
                return ToolResult(
                    text_result_for_llm="GUI launch request timed out",
                    result_type="error",
                )

        gui_tool = Tool(
            name="launch_gui",
            description=(
                "Launch a GUI application on the user's desktop (Wayland/Hyprland). "
                "Requires user approval. Use for browsers, editors, media players, etc. "
                "Example: launch_gui(command='firefox https://example.com', reason='Open docs')"
            ),
            handler=_gui_handler,
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The command to launch (e.g., 'firefox', 'code .')",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why this GUI app needs to be launched",
                    },
                },
                "required": ["command"],
            },
            skip_permission=True,
        )
        custom_tools.append(gui_tool)

        # Custom tool: screenshot — capture the user's screen
        async def _screenshot_handler(invocation: object) -> ToolResult:
            if not ipc or not ipc.is_connected:
                return ToolResult(
                    text_result_for_llm="Error: not connected to orchestrator",
                    result_type="error",
                )
            try:
                response = await ipc.request(
                    Message(
                        type=MessageType.SCREENSHOT_REQUEST,
                        payload={},
                    ),
                    timeout=30.0,
                )
                rp = response.payload
                if rp.get("error"):
                    return ToolResult(
                        text_result_for_llm=f"Screenshot failed: {rp['error']}",
                        result_type="error",
                    )
                path = rp.get("path", "")
                return ToolResult(
                    text_result_for_llm=(
                        f"Screenshot saved to: {path}\n"
                        "Use send_file to show it to the user."
                    ),
                )
            except asyncio.TimeoutError:
                return ToolResult(
                    text_result_for_llm="Screenshot request timed out",
                    result_type="error",
                )

        screenshot_tool = Tool(
            name="screenshot",
            description=(
                "Take a screenshot of the user's desktop. The image is saved "
                "to your workspace. Use send_file to share it with the user. "
                "No approval needed (read-only operation)."
            ),
            handler=_screenshot_handler,
            parameters={"type": "object", "properties": {}},
            skip_permission=True,
        )
        custom_tools.append(screenshot_tool)

        # Custom tool: remember — store a memory for future sessions
        async def _remember_handler(invocation: object) -> ToolResult:
            args = getattr(invocation, "arguments", {}) or {}
            content = args.get("content", "")
            category = args.get("category", "other")
            is_key = args.get("is_key", False)
            if not content:
                return ToolResult(
                    text_result_for_llm="Error: 'content' parameter is required",
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
                        type=MessageType.MEMORY_STORE,
                        payload={"content": content, "category": category, "is_key": is_key},
                    ),
                    timeout=10.0,
                )
                rp = response.payload
                if rp.get("error"):
                    return ToolResult(
                        text_result_for_llm=f"Memory store failed: {rp['error']}",
                        result_type="error",
                    )
                mem = rp.get("memory", {})
                key_str = " (key memory)" if is_key else ""
                return ToolResult(
                    text_result_for_llm=f"Memory stored{key_str}: [{category}] {content[:80]}... (id: {mem.get('id', '?')})",
                )
            except asyncio.TimeoutError:
                return ToolResult(
                    text_result_for_llm="Memory store timed out",
                    result_type="error",
                )

        remember_tool = Tool(
            name="remember",
            description=(
                "Store a memory that persists across sessions. Use for things worth "
                "remembering: user preferences, coding style, personal facts, project "
                "decisions. Key memories (is_key=true) are included in every future "
                "session's context."
            ),
            handler=_remember_handler,
            parameters={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The memory content (concise note)",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["personal", "technical", "project", "workflow", "debug", "other"],
                        "description": "Memory category",
                    },
                    "is_key": {
                        "type": "boolean",
                        "description": "If true, included in every future session's system prompt",
                    },
                },
                "required": ["content"],
            },
            skip_permission=True,
        )
        custom_tools.append(remember_tool)

        # Custom tool: recall — search memories from past sessions
        async def _recall_handler(invocation: object) -> ToolResult:
            args = getattr(invocation, "arguments", {}) or {}
            keyword = args.get("keyword", "")
            category = args.get("category", "")
            if not ipc or not ipc.is_connected:
                return ToolResult(
                    text_result_for_llm="Error: not connected to orchestrator",
                    result_type="error",
                )
            try:
                response = await ipc.request(
                    Message(
                        type=MessageType.MEMORY_QUERY,
                        payload={"keyword": keyword, "category": category},
                    ),
                    timeout=10.0,
                )
                rp = response.payload
                if rp.get("error"):
                    return ToolResult(
                        text_result_for_llm=f"Memory query failed: {rp['error']}",
                        result_type="error",
                    )
                memories = rp.get("memories", [])
                if not memories:
                    return ToolResult(text_result_for_llm="No memories found.")
                lines = []
                for m in memories:
                    key = "🔑" if m.get("is_key_memory") else ""
                    lines.append(f"- [{m['category']}]{key} {m['content']} (id:{m['id']})")
                return ToolResult(
                    text_result_for_llm=f"Found {len(memories)} memories:\n" + "\n".join(lines),
                )
            except asyncio.TimeoutError:
                return ToolResult(
                    text_result_for_llm="Memory query timed out",
                    result_type="error",
                )

        recall_tool = Tool(
            name="recall",
            description=(
                "Search your memories from past sessions. Use to look up user "
                "preferences, past decisions, project knowledge, or anything "
                "you've previously stored with 'remember'."
            ),
            handler=_recall_handler,
            parameters={
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "Search keyword (substring match on content)",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["personal", "technical", "project", "workflow", "debug", "other"],
                        "description": "Filter by category",
                    },
                },
            },
            skip_permission=True,
        )
        custom_tools.append(recall_tool)

        # Custom tool: forget — delete a memory
        async def _forget_handler(invocation: object) -> ToolResult:
            args = getattr(invocation, "arguments", {}) or {}
            memory_id = args.get("memory_id", "")
            if not memory_id:
                return ToolResult(
                    text_result_for_llm="Error: 'memory_id' parameter is required",
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
                        type=MessageType.MEMORY_DELETE,
                        payload={"memory_id": memory_id},
                    ),
                    timeout=10.0,
                )
                rp = response.payload
                if rp.get("ok"):
                    return ToolResult(text_result_for_llm=f"Memory {memory_id} deleted.")
                return ToolResult(
                    text_result_for_llm=f"Memory {memory_id} not found.",
                    result_type="error",
                )
            except asyncio.TimeoutError:
                return ToolResult(
                    text_result_for_llm="Memory delete timed out",
                    result_type="error",
                )

        forget_tool = Tool(
            name="forget",
            description="Delete a memory by its ID. Use recall first to find the ID.",
            handler=_forget_handler,
            parameters={
                "type": "object",
                "properties": {
                    "memory_id": {
                        "type": "string",
                        "description": "The memory ID to delete",
                    },
                },
                "required": ["memory_id"],
            },
            skip_permission=True,
        )
        custom_tools.append(forget_tool)

        # Custom tool: spawn_sub_agent — request the orchestrator to spawn a sub-agent
        async def _spawn_sub_agent_handler(invocation: object) -> ToolResult:
            args = getattr(invocation, "arguments", {}) or {}
            name = args.get("name", "sub-agent")
            purpose = args.get("purpose", "")
            has_network = args.get("has_network", False)
            has_workspace = args.get("has_workspace", False)
            profile = args.get("profile", "light")

            if not purpose:
                return ToolResult(
                    text_result_for_llm="Error: purpose is required",
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
                        type=MessageType.SUB_AGENT_REQUEST,
                        payload={
                            "name": name,
                            "purpose": purpose,
                            "has_network": has_network,
                            "has_workspace": has_workspace,
                            "profile": profile,
                        },
                    ),
                    timeout=60.0,
                )
                rp = response.payload
                if rp.get("error"):
                    return ToolResult(
                        text_result_for_llm=f"Sub-agent spawn failed: {rp['error']}",
                        result_type="error",
                    )
                return ToolResult(
                    text_result_for_llm=(
                        f"Sub-agent '{rp.get('name', name)}' spawned successfully.\n"
                        f"Session: {rp.get('sub_agent_id', 'unknown')}\n"
                        f"Status: {rp.get('status', 'running')}\n"
                        f"The sub-agent is working on: {purpose[:200]}"
                    ),
                )
            except asyncio.TimeoutError:
                return ToolResult(
                    text_result_for_llm="Sub-agent spawn timed out (60s)",
                    result_type="error",
                )

        spawn_sub_agent_tool = Tool(
            name="spawn_sub_agent",
            description=(
                "Spawn a sub-agent to work on a task in parallel. The sub-agent "
                "runs in its own container and communicates results via a Matrix "
                "thread. Use for delegating research, code review, or independent "
                "tasks. Max 3 concurrent sub-agents."
            ),
            handler=_spawn_sub_agent_handler,
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Short name for the sub-agent (e.g., 'researcher', 'reviewer')",
                    },
                    "purpose": {
                        "type": "string",
                        "description": "What the sub-agent should do — this becomes its initial prompt",
                    },
                    "has_network": {
                        "type": "boolean",
                        "description": "Whether the sub-agent gets network access (default: false)",
                    },
                    "has_workspace": {
                        "type": "boolean",
                        "description": "Whether the sub-agent gets access to the project workspace (default: false)",
                    },
                    "profile": {
                        "type": "string",
                        "description": "Container profile to use (default: 'light')",
                    },
                },
                "required": ["name", "purpose"],
            },
        )
        custom_tools.append(spawn_sub_agent_tool)

        # ------------------------------------------------------------------
        # Git workstream tools — for local dev collaboration
        # ------------------------------------------------------------------

        async def _run_git(args_list: list[str], cwd: str = "/workspace") -> tuple[int, str, str]:
            """Run a git command and return (returncode, stdout, stderr)."""
            try:
                proc = await asyncio.create_subprocess_exec(
                    "git", *args_list,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
                return proc.returncode, stdout.decode().strip(), stderr.decode().strip()
            except asyncio.TimeoutError:
                return -1, "", "Git command timed out"
            except FileNotFoundError:
                return -1, "", "git not found in container"

        async def _git_status_handler(invocation: object) -> ToolResult:
            args = getattr(invocation, "arguments", {}) or {}
            cwd = args.get("path", "/workspace")

            rc, branch_out, _ = await _run_git(["branch", "--show-current"], cwd)
            branch = branch_out or "(detached HEAD)"

            rc2, status_out, _ = await _run_git(["status", "--short"], cwd)
            rc3, log_out, _ = await _run_git(
                ["log", "--oneline", "-5", "--no-decorate"], cwd,
            )

            result = f"Branch: {branch}\n"
            if status_out:
                result += f"\nChanges:\n{status_out}\n"
            else:
                result += "\nWorking tree clean.\n"
            if log_out:
                result += f"\nRecent commits:\n{log_out}"

            return ToolResult(text_result_for_llm=result)

        git_status_tool = Tool(
            name="git_status",
            description=(
                "Show git status: current branch, uncommitted changes, and recent "
                "commit history. Use to understand the current state of the repo."
            ),
            handler=_git_status_handler,
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Repository path (default: /workspace)",
                    },
                },
            },
            skip_permission=True,
        )
        custom_tools.append(git_status_tool)

        async def _git_branch_handler(invocation: object) -> ToolResult:
            args = getattr(invocation, "arguments", {}) or {}
            action = args.get("action", "list")
            name = args.get("name", "")
            cwd = args.get("path", "/workspace")

            if action == "list":
                rc, out, err = await _run_git(["branch", "-a"], cwd)
                return ToolResult(text_result_for_llm=out or err or "No branches found")

            elif action == "create":
                if not name:
                    return ToolResult(
                        text_result_for_llm="Error: branch name required",
                        result_type="error",
                    )
                rc, out, err = await _run_git(["checkout", "-b", name], cwd)
                if rc == 0:
                    return ToolResult(text_result_for_llm=f"Created and switched to branch: {name}")
                return ToolResult(text_result_for_llm=f"Error: {err}", result_type="error")

            elif action == "switch":
                if not name:
                    return ToolResult(
                        text_result_for_llm="Error: branch name required",
                        result_type="error",
                    )
                rc, out, err = await _run_git(["checkout", name], cwd)
                if rc == 0:
                    return ToolResult(text_result_for_llm=f"Switched to branch: {name}")
                return ToolResult(text_result_for_llm=f"Error: {err}", result_type="error")

            elif action == "delete":
                if not name:
                    return ToolResult(
                        text_result_for_llm="Error: branch name required",
                        result_type="error",
                    )
                rc, out, err = await _run_git(["branch", "-d", name], cwd)
                if rc == 0:
                    return ToolResult(text_result_for_llm=f"Deleted branch: {name}")
                return ToolResult(text_result_for_llm=f"Error: {err}", result_type="error")

            return ToolResult(
                text_result_for_llm=f"Unknown action: {action}",
                result_type="error",
            )

        git_branch_tool = Tool(
            name="git_branch",
            description=(
                "Manage git branches: list all branches, create a new feature "
                "branch, switch branches, or delete a merged branch."
            ),
            handler=_git_branch_handler,
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "create", "switch", "delete"],
                        "description": "Branch operation to perform",
                    },
                    "name": {
                        "type": "string",
                        "description": "Branch name (for create/switch/delete)",
                    },
                    "path": {
                        "type": "string",
                        "description": "Repository path (default: /workspace)",
                    },
                },
                "required": ["action"],
            },
            skip_permission=True,
        )
        custom_tools.append(git_branch_tool)

        async def _git_commit_handler(invocation: object) -> ToolResult:
            args = getattr(invocation, "arguments", {}) or {}
            message = args.get("message", "")
            files = args.get("files", [])
            all_changes = args.get("all", False)
            cwd = args.get("path", "/workspace")

            if not message:
                return ToolResult(
                    text_result_for_llm="Error: commit message required",
                    result_type="error",
                )

            # Stage files
            if all_changes:
                rc, _, err = await _run_git(["add", "-A"], cwd)
            elif files:
                rc, _, err = await _run_git(["add", "--"] + files, cwd)
            else:
                # Stage all tracked changes by default
                rc, _, err = await _run_git(["add", "-u"], cwd)

            if rc != 0:
                return ToolResult(
                    text_result_for_llm=f"Failed to stage: {err}",
                    result_type="error",
                )

            # Check there are staged changes
            rc, diff_out, _ = await _run_git(["diff", "--cached", "--stat"], cwd)
            if not diff_out:
                return ToolResult(
                    text_result_for_llm="Nothing to commit — no staged changes.",
                )

            # Commit
            rc, out, err = await _run_git(["commit", "-m", message], cwd)
            if rc == 0:
                # Get the short hash
                rc2, hash_out, _ = await _run_git(["rev-parse", "--short", "HEAD"], cwd)
                return ToolResult(
                    text_result_for_llm=f"Committed: {hash_out} {message}\n\n{diff_out}",
                )
            return ToolResult(text_result_for_llm=f"Commit failed: {err}", result_type="error")

        git_commit_tool = Tool(
            name="git_commit",
            description=(
                "Stage and commit changes. By default stages all tracked file "
                "changes. Use 'files' to stage specific files, or 'all' to "
                "include untracked files."
            ),
            handler=_git_commit_handler,
            parameters={
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "Commit message",
                    },
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Specific files to stage (optional)",
                    },
                    "all": {
                        "type": "boolean",
                        "description": "Stage all changes including untracked (default: false)",
                    },
                    "path": {
                        "type": "string",
                        "description": "Repository path (default: /workspace)",
                    },
                },
                "required": ["message"],
            },
        )
        custom_tools.append(git_commit_tool)

        async def _git_push_handler(invocation: object) -> ToolResult:
            args = getattr(invocation, "arguments", {}) or {}
            remote = args.get("remote", "origin")
            branch = args.get("branch", "")
            set_upstream = args.get("set_upstream", False)
            cwd = args.get("path", "/workspace")

            cmd = ["push"]
            if set_upstream:
                cmd.append("--set-upstream")
            cmd.append(remote)
            if branch:
                cmd.append(branch)

            rc, out, err = await _run_git(cmd, cwd)
            combined = (out + "\n" + err).strip()
            if rc == 0:
                return ToolResult(text_result_for_llm=f"Push successful.\n{combined}")
            return ToolResult(
                text_result_for_llm=f"Push failed:\n{combined}",
                result_type="error",
            )

        git_push_tool = Tool(
            name="git_push",
            description="Push commits to remote repository.",
            handler=_git_push_handler,
            parameters={
                "type": "object",
                "properties": {
                    "remote": {
                        "type": "string",
                        "description": "Remote name (default: origin)",
                    },
                    "branch": {
                        "type": "string",
                        "description": "Branch to push (default: current branch)",
                    },
                    "set_upstream": {
                        "type": "boolean",
                        "description": "Set upstream tracking (for new branches)",
                    },
                    "path": {
                        "type": "string",
                        "description": "Repository path (default: /workspace)",
                    },
                },
            },
        )
        custom_tools.append(git_push_tool)

        async def _git_diff_handler(invocation: object) -> ToolResult:
            args = getattr(invocation, "arguments", {}) or {}
            staged = args.get("staged", False)
            target = args.get("target", "")
            stat_only = args.get("stat_only", False)
            cwd = args.get("path", "/workspace")

            cmd = ["diff"]
            if staged:
                cmd.append("--cached")
            if stat_only:
                cmd.append("--stat")
            if target:
                cmd.append(target)

            rc, out, err = await _run_git(cmd, cwd)
            if not out and not err:
                return ToolResult(text_result_for_llm="No differences found.")
            # Truncate large diffs
            if len(out) > 8000:
                out = out[:8000] + f"\n\n... (truncated, {len(out)} chars total)"
            return ToolResult(text_result_for_llm=out or err)

        git_diff_tool = Tool(
            name="git_diff",
            description=(
                "Show git diff — unstaged changes by default, or staged changes, "
                "or diff against a branch/commit."
            ),
            handler=_git_diff_handler,
            parameters={
                "type": "object",
                "properties": {
                    "staged": {
                        "type": "boolean",
                        "description": "Show staged (cached) changes",
                    },
                    "target": {
                        "type": "string",
                        "description": "Compare against branch, tag, or commit SHA",
                    },
                    "stat_only": {
                        "type": "boolean",
                        "description": "Show only file change statistics",
                    },
                    "path": {
                        "type": "string",
                        "description": "Repository path (default: /workspace)",
                    },
                },
            },
            skip_permission=True,
        )
        custom_tools.append(git_diff_tool)

        async def _git_pr_handler(invocation: object) -> ToolResult:
            """Create a GitHub PR using the GitHub API."""
            args = getattr(invocation, "arguments", {}) or {}
            title = args.get("title", "")
            body = args.get("body", "")
            base = args.get("base", "main")
            head = args.get("head", "")
            cwd = args.get("path", "/workspace")

            if not title:
                return ToolResult(
                    text_result_for_llm="Error: PR title required",
                    result_type="error",
                )

            # Get current branch as head if not specified
            if not head:
                rc, head, _ = await _run_git(["branch", "--show-current"], cwd)
                if not head:
                    return ToolResult(
                        text_result_for_llm="Error: not on a branch",
                        result_type="error",
                    )

            # Get remote URL to extract owner/repo
            rc, remote_url, _ = await _run_git(
                ["config", "--get", "remote.origin.url"], cwd,
            )
            if not remote_url:
                return ToolResult(
                    text_result_for_llm="Error: no remote origin configured",
                    result_type="error",
                )

            # Parse owner/repo from URL (handles both HTTPS and SSH)
            import re
            match = re.search(r"[:/]([^/]+)/([^/.]+?)(?:\.git)?$", remote_url)
            if not match:
                return ToolResult(
                    text_result_for_llm=f"Error: cannot parse remote URL: {remote_url}",
                    result_type="error",
                )
            owner, repo = match.group(1), match.group(2)

            token = os.environ.get("GITHUB_TOKEN", "")
            if not token:
                return ToolResult(
                    text_result_for_llm="Error: GITHUB_TOKEN not available",
                    result_type="error",
                )

            # Create PR via GitHub API
            import json as _json
            try:
                proc = await asyncio.create_subprocess_exec(
                    "curl", "-s", "-X", "POST",
                    f"https://api.github.com/repos/{owner}/{repo}/pulls",
                    "-H", f"Authorization: token {token}",
                    "-H", "Accept: application/vnd.github.v3+json",
                    "-d", _json.dumps({
                        "title": title,
                        "body": body,
                        "head": head,
                        "base": base,
                    }),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
                resp = _json.loads(stdout.decode())

                if "html_url" in resp:
                    return ToolResult(
                        text_result_for_llm=(
                            f"PR created: {resp['html_url']}\n"
                            f"#{resp['number']} — {resp['title']}"
                        ),
                    )
                elif "message" in resp:
                    return ToolResult(
                        text_result_for_llm=f"GitHub API error: {resp['message']}",
                        result_type="error",
                    )
                return ToolResult(
                    text_result_for_llm=f"Unexpected response: {stdout.decode()[:500]}",
                    result_type="error",
                )
            except Exception as e:
                return ToolResult(
                    text_result_for_llm=f"PR creation failed: {e}",
                    result_type="error",
                )

        git_pr_tool = Tool(
            name="git_pr",
            description=(
                "Create a GitHub Pull Request from the current branch. "
                "Pushes first if needed. Uses the GITHUB_TOKEN for authentication."
            ),
            handler=_git_pr_handler,
            parameters={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "PR title",
                    },
                    "body": {
                        "type": "string",
                        "description": "PR description/body (markdown)",
                    },
                    "base": {
                        "type": "string",
                        "description": "Base branch to merge into (default: main)",
                    },
                    "head": {
                        "type": "string",
                        "description": "Head branch (default: current branch)",
                    },
                    "path": {
                        "type": "string",
                        "description": "Repository path (default: /workspace)",
                    },
                },
                "required": ["title"],
            },
        )
        custom_tools.append(git_pr_tool)

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

    print(f"[agent] Starting agent: {session_name} ({session_id})", file=sys.stderr)
    print(f"[agent] Socket: {socket_path}", file=sys.stderr)

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
            print(f"[agent] Waiting for socket... ({retries}/10)", file=sys.stderr)
            await asyncio.sleep(1)
    else:
        print("[agent] Failed to connect to orchestrator", file=sys.stderr)
        sys.exit(1)

    print("[agent] Connected to orchestrator", file=sys.stderr)

    # Try to init Copilot SDK
    sdk_result = await try_init_copilot(ipc=ipc)
    listener_ctl = None
    if sdk_result:
        sdk_client, sdk_session = sdk_result
        print("[agent] Copilot SDK initialized", file=sys.stderr)
        # Register persistent event listener (handles background agents too)
        try:
            listener_ctl = setup_session_listener(ipc, sdk_session, loop)
            print("[agent] Persistent event listener registered", file=sys.stderr)
        except ImportError:
            print("[agent] SessionEventType not available, running in echo mode", file=sys.stderr)
            sdk_client, sdk_session = None, None
    else:
        sdk_client, sdk_session = None, None
        print("[agent] Running in echo mode (no Copilot SDK)", file=sys.stderr)

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

    async def on_scheduled_trigger(msg: Message) -> Message | None:
        """Handle scheduled callback — forward to SDK as a system message."""
        reason = msg.payload.get("reason", "Scheduled callback")
        sched_id = msg.payload.get("id", "unknown")
        ts = msg.payload.get("timestamp", "")
        content = (
            f"<current_datetime>{ts}</current_datetime>\n\n"
            f"[Scheduled callback: {sched_id}] {reason}"
        )
        synth = Message(
            type=MessageType.USER_MESSAGE,
            payload={"content": content, "sender": "scheduler", "timestamp": ts},
        )
        await handle_user_message(ipc, sdk_session, synth, loop, listener_ctl)
        return None

    async def on_shutdown(msg: Message) -> Message | None:
        print("[agent] Shutdown requested", file=sys.stderr)
        await ipc.disconnect()
        return None

    async def on_dream_request(msg: Message) -> Message | None:
        """Auto-dreaming: extract noteworthy memories from context."""
        print("[agent] Dream request received — extracting memories", file=sys.stderr)
        reason = msg.payload.get("reason", "")

        # Build a focused extraction prompt
        dream_prompt = (
            "Review the conversation so far and extract any noteworthy information "
            "that should be remembered across sessions. Look for:\n"
            "- Personal facts (name, family, preferences, timezone)\n"
            "- Technical preferences (languages, frameworks, code style)\n"
            "- Project knowledge (architecture, conventions, key decisions)\n"
            "- Workflow patterns (how the user likes to work)\n"
            "- Debugging insights (solutions to problems encountered)\n\n"
            "Return a JSON array of objects, each with:\n"
            '  {"content": "concise note", "category": "personal|technical|project|workflow|debug|other", "is_key": true/false}\n\n'
            "Only include genuinely useful information. Key memories (is_key=true) "
            "are loaded into every future session. Be selective.\n"
            "If nothing noteworthy, return an empty array: []"
        )

        try:
            # Use the SDK to extract memories from the current context
            turn = sdk_session.send(
                dream_prompt,
                options={"model": "gpt-4o-mini"},  # use smaller model for extraction
            )
            response_text = ""
            async for event in turn:
                etype = str(getattr(event, "type", ""))
                if "text_message_content" in etype:
                    text = getattr(event, "text", "")
                    if text:
                        response_text = text

            # Parse the JSON response
            import json as _json
            # Extract JSON from response (may have markdown code blocks)
            cleaned = response_text.strip()
            if cleaned.startswith("```"):
                lines = cleaned.split("\n")
                cleaned = "\n".join(lines[1:-1])
            memories = _json.loads(cleaned)

            if memories and isinstance(memories, list):
                print(f"[agent] Auto-dreaming extracted {len(memories)} memories", file=sys.stderr)
                await ipc.send(Message(
                    type=MessageType.DREAM_COMPLETE,
                    payload={"memories": memories},
                ))
            else:
                print("[agent] Auto-dreaming: nothing noteworthy found", file=sys.stderr)
        except Exception as e:
            print(f"[agent] Auto-dreaming failed: {e}", file=sys.stderr)

        return None

    async def on_file_change(msg: Message) -> Message | None:
        """Handle file change notifications from workspace watcher."""
        if not session:
            return None
        changes = msg.payload.get("changes", [])
        count = msg.payload.get("count", 0)
        if not changes:
            return None

        # Format the notification as a brief user-facing message
        summary_lines = []
        for c in changes[:10]:
            icon = {"created": "➕", "modified": "✏️", "deleted": "🗑️"}.get(c["type"], "?")
            summary_lines.append(f"{icon} {c['path']}")
        if count > 10:
            summary_lines.append(f"... and {count - 10} more")

        notification = (
            f"📁 **Workspace files changed externally** ({count} files):\n"
            + "\n".join(summary_lines)
        )

        # Send as a system-level notification to the session
        print(f"[agent] File changes detected: {count} files", file=sys.stderr)
        try:
            await session.send_user_message(
                notification,
                reply_to=None,
            )
        except Exception as e:
            print(f"[agent] Failed to notify about file changes: {e}", file=sys.stderr)

        return None

    ipc.on_message(MessageType.USER_MESSAGE, on_user_message)
    ipc.on_message(MessageType.SCHEDULE_TRIGGER, on_scheduled_trigger)
    ipc.on_message(MessageType.TIMER_TRIGGER, on_scheduled_trigger)
    ipc.on_message(MessageType.SHUTDOWN, on_shutdown)
    ipc.on_message(MessageType.DREAM_REQUEST, on_dream_request)
    ipc.on_message(MessageType.FILE_CHANGE, on_file_change)

    print("[agent] Ready and listening", file=sys.stderr)

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
    print("[agent] Shut down", file=sys.stderr)


if __name__ == "__main__":
    # Ensure stdout/stderr are unbuffered so logs appear in podman logs
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
    asyncio.run(main())
