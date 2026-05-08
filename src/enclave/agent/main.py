"""Agent entry point — runs inside the podman container.

Connects to the orchestrator via IPC socket, initializes the Copilot SDK,
and routes messages between the orchestrator and the AI model.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from collections import Counter, deque
from pathlib import Path
from typing import TYPE_CHECKING

from enclave.agent.ipc_client import IPCClient
from enclave.common.protocol import Message, MessageType

if TYPE_CHECKING:
    from copilot import CopilotClient as _CopilotClient
    from copilot.session import CopilotSession as _CopilotSession


class AgentState:
    """Mutable container for agent runtime state.

    Closures in main() reference this object so that session recovery can
    swap sdk_client / sdk_session and all handlers immediately see the new
    references without re-binding.
    """

    __slots__ = (
        "sdk_client", "sdk_session", "listener_ctl",
        "ipc", "loop", "working_directory",
        "turn_active", "turn_phase",
        "pending_interrupt", "turns_since_enqueue", "enqueue_time",
        "pending_messages", "queued_user_messages",
        # Doom loop detection
        "task_start_time", "consecutive_turns", "tool_failures",
        "consecutive_failures",
        "recent_edit_targets", "recent_bash_commands",
        "doom_loop_nudged_at", "doom_loop_nudge_count",
        # Auto-continue lifecycle
        "task_done", "asked_user", "auto_continue_handle",
        # Mimir memory backend
        "mimir_enabled", "mimir_killswitch_reason",
        "mimir_workspace", "mimir_cli_bin", "mimir_librarian_bin",
        "mimir_failure_count",
        "_mimir_submit_draft",
        "mimir_recent_user_msgs", "mimir_recent_tool_calls",
    )

    # After this many turns with a pending message, deny tool calls to nudge
    NUDGE_TURNS = 10
    # Hard abort safety net (if nudging doesn't work)
    MAX_TURNS_BEFORE_INTERRUPT = 100
    MAX_TIME_BEFORE_INTERRUPT = 300.0  # 5 minutes

    # Doom loop detection thresholds
    DOOM_LOOP_TIME_GATE = 300.0  # 5 minutes of continuous work before checking
    DOOM_LOOP_MIN_TURNS = 30  # don't even consider nudging below this turn count
    DOOM_LOOP_CONSECUTIVE_FAILURES = 3  # back-to-back tool failures signal distress
    DOOM_LOOP_WINDOWED_EDITS = 5  # same file edited this many times in recent edits
    DOOM_LOOP_EDIT_WINDOW = 15  # how many recent edits we keep for windowed detection
    DOOM_LOOP_WINDOWED_BASH = 4  # same bash command run this many times in recent bash
    DOOM_LOOP_BASH_WINDOW = 10  # how many recent bash commands we keep
    DOOM_LOOP_LONG_TASK_TURNS = 60  # long-runner gate (paired with 15min elapsed)
    DOOM_LOOP_LONG_TASK_TIME = 900.0  # 15 minutes
    DOOM_LOOP_COOLDOWN_TURNS = 40  # minimum turns between nudges at the first level

    def __init__(self) -> None:
        self.sdk_client: _CopilotClient | None = None
        self.sdk_session: _CopilotSession | None = None
        self.listener_ctl: object | None = None
        self.ipc: IPCClient | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.working_directory: str = "/workspace"
        # Turn tracking for smart message injection
        self.turn_active: bool = False
        self.turn_phase: str = "idle"  # idle | thinking | tool | responding
        # Pending message interrupt tracking
        self.pending_interrupt: bool = False
        self.turns_since_enqueue: int = 0
        self.enqueue_time: float = 0.0
        self.pending_messages: list[str] = []  # stored content for check_messages tool
        self.queued_user_messages: list[tuple[str, list | None]] = []  # (content, attachments) batched for next turn
        # Doom loop detection
        self.task_start_time: float = 0.0  # monotonic time of first turn after idle
        self.consecutive_turns: int = 0
        self.tool_failures: int = 0
        self.consecutive_failures: int = 0  # resets on any tool success
        # Bounded deques so we compute signals over a recent window rather
        # than lifetime totals (avoids false positives during long refactors).
        self.recent_edit_targets: deque[str] = deque(
            maxlen=AgentState.DOOM_LOOP_EDIT_WINDOW
        )
        self.recent_bash_commands: deque[str] = deque(
            maxlen=AgentState.DOOM_LOOP_BASH_WINDOW
        )
        self.doom_loop_nudged_at: int = 0
        self.doom_loop_nudge_count: int = 0  # exponential backoff multiplier
        # Auto-continue lifecycle
        self.task_done: bool = False  # agent explicitly called mark_done
        self.asked_user: bool = False  # agent explicitly called ask_user
        self.auto_continue_handle: object | None = None  # pending call_later handle

        # Mimir memory backend (read from env at startup; orchestrator
        # populates these via container env when mimir.enabled=True).
        self.mimir_enabled: bool = (
            os.environ.get("ENCLAVE_MIMIR_ENABLED", "0").strip().lower()
            in {"1", "true", "yes", "on", "enabled"}
        )
        self.mimir_killswitch_reason: str | None = None
        self.mimir_failure_count: int = 0
        # Resolve workspace, cli, librarian paths from env (with fallbacks).
        agent_name = os.environ.get("ENCLAVE_MIMIR_AGENT_NAME", "brook")
        ws_root = os.environ.get(
            "ENCLAVE_MIMIR_WORKSPACE_ROOT",
            "/home/agent/.local/share/enclave/mimir",
        )
        self.mimir_workspace: str = f"{ws_root.rstrip('/')}/{agent_name}"
        self.mimir_cli_bin: str = os.environ.get(
            "ENCLAVE_MIMIR_CLI_BIN", "/usr/local/bin/mimir-cli"
        )
        self.mimir_librarian_bin: str = os.environ.get(
            "ENCLAVE_MIMIR_LIBRARIAN_BIN", "/usr/local/bin/mimir-librarian"
        )
        # Wired by setup_custom_tools when Mimir tools are registered.
        # Used by the compaction hook to submit a snapshot draft.
        self._mimir_submit_draft = None  # type: ignore[assignment]
        # Rolling buffers used by the compaction hook to build a summary
        # before the SDK summarises away the rich context. Bounded so a
        # long-running session doesn't accumulate unboundedly.
        self.mimir_recent_user_msgs: deque[tuple[float, str]] = deque(maxlen=8)
        self.mimir_recent_tool_calls: deque[tuple[float, str, str]] = deque(maxlen=40)


async def _mimir_compaction_submit(
    *,
    submit: object,
    msgs: list[tuple[float, str]],
    tools: list[tuple[float, str, str]],
) -> None:
    """Build a compaction-window summary and submit as a Mimir draft.

    Called fire-and-forget from the SESSION_COMPACTION_START handler.
    All exceptions are swallowed (logged) — never let a Mimir failure
    interrupt compaction. The corresponding killswitch lives on the
    submit closure, which trips after 3 consecutive failures.
    """
    try:
        import datetime as _dt
        from collections import Counter as _Counter
        if not msgs and not tools:
            return
        # Build a structured prose summary. Cue the librarian that this
        # is a compaction-window snapshot — durability="instruction"
        # because it captures intent + recent activity rather than a
        # witnessed milestone.
        parts: list[str] = []
        parts.append(
            "This is a Brook session-compaction snapshot. The SDK is "
            "about to compact; capturing recent context so it survives."
        )
        if msgs:
            parts.append(
                f"\nRecent user messages ({len(msgs)}, oldest first):"
            )
            for ts, content in msgs:
                ts_iso = _dt.datetime.utcfromtimestamp(ts).strftime("%H:%M:%S")
                snippet = content.replace("\n", " ").strip()[:400]
                parts.append(f"- [{ts_iso}] {snippet}")
        if tools:
            tool_counts = _Counter(t[1] for t in tools)
            top = ", ".join(
                f"{name}×{n}" for name, n in tool_counts.most_common(8)
            )
            parts.append(
                f"\nRecent tool activity ({len(tools)} calls): {top}."
            )
            recent_details = [
                f"{name}: {detail}" for _, name, detail in tools[-10:] if detail
            ]
            if recent_details:
                parts.append("Last 10 tool details:")
                parts.extend(f"- {d}" for d in recent_details)
        prose = "\n".join(parts)
        ok, msg = await submit(
            prose=prose,
            durability="instruction",
            tags=[
                "compaction-snapshot",
                f"user-msgs:{len(msgs)}",
                f"tool-calls:{len(tools)}",
            ],
            source_surface="agent-export",
        )
        if not ok:
            print(f"[agent] Mimir compaction submit failed: {msg}", file=sys.stderr)
    except Exception as e:
        print(f"[agent] Mimir compaction submit error: {e}", file=sys.stderr)


def setup_session_listener(
    ipc: IPCClient,
    sdk_session: _CopilotSession,
    loop: asyncio.AbstractEventLoop,
    agent_state: AgentState | None = None,
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
    accumulated_thinking: list[str] = []

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

        def _set_phase(phase: str, active: bool | None = None) -> None:
            if agent_state is not None:
                agent_state.turn_phase = phase
                if active is not None:
                    agent_state.turn_active = active

        if etype == SessionEventType.ASSISTANT_MESSAGE_DELTA:
            _set_phase("responding")
            if accumulated_thinking:
                accumulated_thinking.clear()
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
            # Clear the delta buffer so the NEXT message's streaming deltas
            # don't inherit this message's final text (which would make the
            # router edit this message with the combined content, appearing
            # to overwrite it).
            final = getattr(data, "content", None) or ""
            accumulated_content.clear()
            if final:
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
            _set_phase("thinking")
            delta = getattr(data, "delta_content", None) or ""
            if delta:
                accumulated_thinking.append(delta)
                full = "".join(accumulated_thinking)
                _fire_and_forget(ipc.send(Message(
                    type=MessageType.AGENT_THINKING,
                    payload={
                        "thinking_content": full,
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
            _set_phase("tool")
            tool_name = getattr(data, "tool_name", None) or getattr(data, "name", None) or "unknown"
            args = getattr(data, "arguments", None) or {}
            if isinstance(args, str):
                try:
                    import json as _json
                    args = _json.loads(args)
                except Exception:
                    args = {}
            description = args.get("description", "") or args.get("intent", "") or args.get("prompt", "")
            # Extract tool-specific detail for richer activity display
            detail = ""
            if tool_name == "bash":
                detail = (args.get("command", "") or "")[:120]
            elif tool_name in ("view", "read"):
                detail = args.get("path", "") or ""
            elif tool_name in ("edit", "create"):
                detail = args.get("path", "") or ""
            elif tool_name == "grep":
                detail = args.get("pattern", "") or ""
            elif tool_name == "glob":
                detail = args.get("pattern", "") or ""
            elif tool_name == "web_fetch":
                detail = args.get("url", "") or ""
            elif tool_name == "web_search":
                detail = args.get("query", "") or ""
            _fire_and_forget(ipc.send(Message(
                type=MessageType.TOOL_START,
                payload={
                    "tool_name": tool_name,
                    "description": str(description)[:200],
                    "detail": str(detail)[:200],
                    "tool_call_id": getattr(data, "tool_call_id", None) or getattr(data, "toolCallId", "") or "",
                    "in_reply_to": reply_to,
                },
                reply_to=reply_to,
            )))
            # Mimir compaction-hook buffer.
            if agent_state and agent_state.mimir_enabled:
                try:
                    agent_state.mimir_recent_tool_calls.append(
                        (time.time(), str(tool_name), str(detail)[:200])
                    )
                except Exception:
                    pass
            # Track edit targets for doom loop detection
            if agent_state and tool_name in ("edit", "create") and detail:
                agent_state.recent_edit_targets.append(detail)
            # Track bash commands (normalised to the first token + first arg)
            # for stereotypy detection — e.g. the agent running the same
            # `make test` repeatedly with no progress.
            if agent_state and tool_name == "bash":
                cmd = (args.get("command", "") or "").strip()
                if cmd:
                    # Normalise to catch minor variations: first two whitespace-
                    # separated tokens, lowercased.
                    norm = " ".join(cmd.split()[:2]).lower()[:80]
                    agent_state.recent_bash_commands.append(norm)

        elif etype == SessionEventType.TOOL_EXECUTION_COMPLETE:
            _set_phase("thinking")  # back to thinking between tools
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
            # Track tool failures for doom loop detection
            if agent_state:
                if success:
                    agent_state.consecutive_failures = 0
                else:
                    agent_state.tool_failures += 1
                    agent_state.consecutive_failures += 1

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
            _set_phase("thinking", active=True)
            accumulated_thinking.clear()
            # Cancel any pending auto-continue — agent is actively working
            if agent_state and agent_state.auto_continue_handle is not None:
                agent_state.auto_continue_handle.cancel()
                agent_state.auto_continue_handle = None
            turn_id = getattr(data, "turn_id", None) or getattr(data, "turnId", "")
            _fire_and_forget(ipc.send(Message(
                type=MessageType.TURN_START,
                payload={"turn_id": str(turn_id), "in_reply_to": reply_to},
                reply_to=reply_to,
            )))

            if agent_state:
                # Doom loop tracking: count turns and check for stuck patterns
                if agent_state.task_start_time == 0.0:
                    agent_state.task_start_time = time.monotonic()
                agent_state.consecutive_turns += 1

                # Doom loop detection — multi-signal with exponential backoff.
                # We require ≥2 independent stuck-signals to fire (and a
                # meaningful turn floor) to avoid nagging during legitimate
                # long tasks. Each re-nudge doubles the cooldown window so
                # the agent isn't spammed if it ignores the first one.
                if agent_state.task_start_time > 0:
                    task_elapsed = time.monotonic() - agent_state.task_start_time
                    turns = agent_state.consecutive_turns
                    # Cooldown grows exponentially with each repeat nudge.
                    cooldown = (
                        AgentState.DOOM_LOOP_COOLDOWN_TURNS
                        * (2 ** agent_state.doom_loop_nudge_count)
                    )
                    turns_since_nudge = turns - agent_state.doom_loop_nudged_at
                    eligible = (
                        task_elapsed >= AgentState.DOOM_LOOP_TIME_GATE
                        and turns >= AgentState.DOOM_LOOP_MIN_TURNS
                        and turns_since_nudge >= cooldown
                    )
                    if eligible:
                        # Compute signals over sliding windows, not lifetime,
                        # so legitimate long tasks don't accrue noise.
                        edit_counts = Counter(agent_state.recent_edit_targets)
                        top_edit_file, top_edit_count = (
                            edit_counts.most_common(1)[0]
                            if edit_counts else ("", 0)
                        )
                        bash_counts = Counter(agent_state.recent_bash_commands)
                        top_bash_cmd, top_bash_count = (
                            bash_counts.most_common(1)[0]
                            if bash_counts else ("", 0)
                        )
                        no_checkpoint = (
                            not agent_state.task_done
                            and not agent_state.asked_user
                        )

                        signals: list[str] = []
                        if agent_state.consecutive_failures >= AgentState.DOOM_LOOP_CONSECUTIVE_FAILURES:
                            signals.append(
                                f"{agent_state.consecutive_failures} tool failures in a row"
                            )
                        if top_edit_count >= AgentState.DOOM_LOOP_WINDOWED_EDITS:
                            signals.append(
                                f"{top_edit_count}× edits to `{top_edit_file}` "
                                f"in the last {AgentState.DOOM_LOOP_EDIT_WINDOW} edits"
                            )
                        if top_bash_count >= AgentState.DOOM_LOOP_WINDOWED_BASH:
                            signals.append(
                                f"`{top_bash_cmd}` run {top_bash_count}× "
                                f"in the last {AgentState.DOOM_LOOP_BASH_WINDOW} bash calls"
                            )
                        if (no_checkpoint
                                and turns >= AgentState.DOOM_LOOP_LONG_TASK_TURNS
                                and task_elapsed >= AgentState.DOOM_LOOP_LONG_TASK_TIME):
                            signals.append(
                                f"{turns} turns ({task_elapsed/60:.0f} min) "
                                "with no `mark_done` or `ask_user`"
                            )

                        if len(signals) >= 2:
                            agent_state.doom_loop_nudged_at = turns
                            agent_state.doom_loop_nudge_count += 1
                            signal_lines = "\n".join(f"  • {s}" for s in signals)
                            print(
                                f"[agent] Doom loop signals tripped "
                                f"(turns={turns}, elapsed={task_elapsed:.0f}s, "
                                f"nudge#{agent_state.doom_loop_nudge_count}):\n"
                                f"{signal_lines}",
                                file=sys.stderr,
                            )
                            # Notify the orchestrator so the user sees it in
                            # Matrix — useful for tuning thresholds later.
                            _fire_and_forget(ipc.send(Message(
                                type=MessageType.STATUS_UPDATE,
                                payload={
                                    "status": "doom_loop_detected",
                                    "turns": turns,
                                    "elapsed_seconds": int(task_elapsed),
                                    "nudge_count": agent_state.doom_loop_nudge_count,
                                    "signals": signals,
                                },
                            )))
                            # Softer, diagnostic message — lets the agent
                            # self-assess rather than commanding a halt.
                            doom_msg = (
                                "[Enclave Coordinator] Quick check-in — a few "
                                "signals suggest you might be looping:\n"
                                f"{signal_lines}\n\n"
                                "If you're making real forward progress, "
                                "carry on. If this rings true, options are: "
                                "`consult_panel` for a fresh perspective, "
                                "`ask_user` if a human decision would unblock "
                                "you, or revert to a known-good state and try "
                                "a different approach."
                            )
                            _fire_and_forget(
                                sdk_session.send(doom_msg, mode="immediate")
                            )

            # Check if a pending user message needs to interrupt this work
            if agent_state and agent_state.pending_interrupt:
                agent_state.turns_since_enqueue += 1
                elapsed = time.monotonic() - agent_state.enqueue_time
                if (agent_state.turns_since_enqueue >= AgentState.MAX_TURNS_BEFORE_INTERRUPT
                        or elapsed >= AgentState.MAX_TIME_BEFORE_INTERRUPT):
                    print(
                        f"[agent] Interrupting for pending message "
                        f"(turns={agent_state.turns_since_enqueue}, "
                        f"elapsed={elapsed:.0f}s)",
                        file=sys.stderr,
                    )
                    agent_state.pending_interrupt = False
                    agent_state.turns_since_enqueue = 0
                    agent_state.pending_messages.clear()
                    _fire_and_forget(sdk_session.abort())

        elif etype == SessionEventType.ASSISTANT_TURN_END:
            _set_phase("idle")
            turn_id = getattr(data, "turn_id", None) or getattr(data, "turnId", "")
            _fire_and_forget(ipc.send(Message(
                type=MessageType.TURN_END,
                payload={"turn_id": str(turn_id), "in_reply_to": reply_to},
                reply_to=reply_to,
            )))

            # Flush queued user messages as a single combined message
            if agent_state and agent_state.queued_user_messages and sdk_session:
                queued = agent_state.queued_user_messages
                agent_state.queued_user_messages = []
                agent_state.pending_messages.clear()
                agent_state.pending_interrupt = False

                # Combine all queued messages into one
                combined_parts = []
                combined_attachments = []
                for content, atts in queued:
                    combined_parts.append(content)
                    if atts:
                        combined_attachments.extend(atts)
                combined = "\n\n---\n\n".join(combined_parts)
                print(f"[agent] Flushing {len(queued)} queued message(s) as one: {combined[:100]}...", file=sys.stderr)

                async def _flush_queued() -> None:
                    try:
                        await sdk_session.send(
                            combined,
                            attachments=combined_attachments or None,
                        )
                    except Exception as e:
                        print(f"[agent] Queued message flush failed: {e}", file=sys.stderr)
                _fire_and_forget(_flush_queued())

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
            # Snapshot recent activity SYNCHRONOUSLY here so the buffers
            # can't race compaction; then submit asynchronously so we
            # don't block the SDK's compaction call. We deliberately
            # capture in this handler rather than the submit coroutine
            # because the SDK may have already pruned by the time the
            # coroutine runs.
            if (
                agent_state
                and agent_state.mimir_enabled
                and not agent_state.mimir_killswitch_reason
                and agent_state._mimir_submit_draft is not None
            ):
                msgs_snapshot = list(agent_state.mimir_recent_user_msgs)
                tools_snapshot = list(agent_state.mimir_recent_tool_calls)
                if msgs_snapshot or tools_snapshot:
                    submit = agent_state._mimir_submit_draft
                    _fire_and_forget(_mimir_compaction_submit(
                        submit=submit,
                        msgs=msgs_snapshot,
                        tools=tools_snapshot,
                    ))

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
            # Forward usage/token events for cost tracking
            if etype_str == "assistant.usage":
                input_tokens = getattr(data, "input_tokens", 0) or getattr(data, "prompt_tokens", 0)
                output_tokens = getattr(data, "output_tokens", 0) or getattr(data, "completion_tokens", 0)
                total_tokens = getattr(data, "total_tokens", 0)
                model = getattr(data, "model", "")
                if input_tokens or output_tokens or total_tokens:
                    _fire_and_forget(ipc.send(Message(
                        type=MessageType.USAGE_REPORT,
                        payload={
                            "input_tokens": input_tokens,
                            "output_tokens": output_tokens,
                            "total_tokens": total_tokens,
                            "model": model or "",
                        },
                    )))
            elif etype_str == "session.idle":
                _set_phase("idle", active=False)
                # Clear interrupt tracking — SDK is idle and will process pending messages
                if agent_state:
                    agent_state.pending_interrupt = False
                    agent_state.turns_since_enqueue = 0
                    agent_state.pending_messages.clear()

                    # Auto-continue: if agent had a multi-turn session and didn't
                    # explicitly mark_done or ask_user, nudge it to continue
                    turns = agent_state.consecutive_turns
                    should_continue = (
                        turns >= 3
                        and not agent_state.task_done
                        and not agent_state.asked_user
                    )

                    # Reset doom loop tracking for next task
                    agent_state.task_start_time = 0.0
                    agent_state.consecutive_turns = 0
                    agent_state.tool_failures = 0
                    agent_state.consecutive_failures = 0
                    agent_state.recent_edit_targets.clear()
                    agent_state.recent_bash_commands.clear()
                    agent_state.doom_loop_nudged_at = 0
                    agent_state.doom_loop_nudge_count = 0
                    # Reset lifecycle flags for next task
                    agent_state.task_done = False
                    agent_state.asked_user = False

                    if should_continue:
                        print(
                            f"[agent] Auto-continue: {turns} turns without "
                            f"mark_done/ask_user, scheduling nudge",
                            file=sys.stderr,
                        )

                        async def _send_continue() -> None:
                            if agent_state.turn_active:
                                return  # already resumed
                            try:
                                await sdk_session.send(
                                    "[Enclave Coordinator] You went idle but didn't "
                                    "call mark_done() or ask_user(). If you have "
                                    "more work to do, please continue with the "
                                    "next item on your plan. If you're finished, "
                                    "call mark_done(). If you need the user's "
                                    "input, call ask_user()."
                                )
                            except Exception as e:
                                print(
                                    f"[agent] Auto-continue send failed: {e}",
                                    file=sys.stderr,
                                )

                        def _fire_continue() -> None:
                            agent_state.auto_continue_handle = None
                            _fire_and_forget(_send_continue())

                        agent_state.auto_continue_handle = loop.call_later(
                            10.0, _fire_continue,
                        )
            else:
                if etype_str not in (
                    "session.tools_updated", "user.message",
                    "session.usage_info", "permission.requested",
                    "permission.completed", "session.background_tasks_changed",
                    "external_tool.requested", "external_tool.completed",
                    "pending_messages.modified", "session.idle",
                    "assistant.streaming_delta",
                ):
                    print(f"[agent] Unhandled event: {etype_str}", file=sys.stderr)

    def set_current_msg(msg_id: str | None) -> None:
        nonlocal current_msg_id
        current_msg_id = msg_id
        accumulated_content.clear()

    unsubscribe = sdk_session.on(on_event)
    # Attach the helper so callers can update the current msg reference.
    unsubscribe.set_current_msg = set_current_msg  # type: ignore[attr-defined]
    return unsubscribe


async def _download_attachments(
    state: AgentState,
    attachments: list[dict],
) -> list[dict]:
    """Resolve media attachments into SDK attachment format.

    If the orchestrator pre-downloaded the file (``local_path`` present),
    reads directly from disk — no IPC round-trip needed.  Falls back to
    an IPC download request for backwards compatibility.

    For images, returns BlobAttachment (base64 inline data).
    For other files, returns FileAttachment with a path reference.
    """
    import base64

    sdk_attachments: list[dict] = []
    attach_dir = Path("/workspace/.attachments")

    for att in attachments:
        url = att.get("url", "")
        filename = att.get("filename", "attachment")
        content_type = att.get("content_type", "")
        encryption = att.get("encryption")
        local_path = att.get("local_path")

        if not url and not local_path:
            continue

        # Fast path: orchestrator already downloaded the file
        if local_path:
            dest = Path(local_path)
            if dest.exists():
                print(f"[agent] Using pre-downloaded attachment: {dest}", file=sys.stderr)
            else:
                print(f"[agent] Pre-downloaded path missing, falling back to IPC: {dest}", file=sys.stderr)
                local_path = None  # fall through to IPC download

        # Slow path: IPC download request (with retries)
        if not local_path:
            dest = attach_dir / filename
            downloaded = False
            for attempt in range(3):
                try:
                    resp = await state.ipc.request(Message(
                        type=MessageType.DOWNLOAD_REQUEST,
                        payload={
                            "url": url,
                            "dest": str(dest),
                            "encryption": encryption,
                        },
                    ), timeout=30.0)
                    if resp.payload.get("downloaded"):
                        downloaded = True
                        break
                    else:
                        print(f"[agent] Download attempt {attempt + 1}/3 failed for {filename}", file=sys.stderr)
                except asyncio.TimeoutError:
                    print(f"[agent] Download attempt {attempt + 1}/3 timed out for {filename}", file=sys.stderr)
                if attempt < 2:
                    await asyncio.sleep(2)

            if not downloaded:
                print(f"[agent] Failed to download attachment: {filename}", file=sys.stderr)
                continue

        # Read the downloaded file
        try:
            data = dest.read_bytes()
        except OSError as e:
            print(f"[agent] Failed to read downloaded file {dest}: {e}", file=sys.stderr)
            continue

        if content_type.startswith("image/"):
            # Inline as BlobAttachment for the model to see
            sdk_attachments.append({
                "type": "blob",
                "data": base64.b64encode(data).decode("ascii"),
                "mimeType": content_type,
                "displayName": filename,
            })
            print(f"[agent] Image attachment ready: {filename} ({len(data)} bytes)", file=sys.stderr)
        else:
            # Keep as file reference
            sdk_attachments.append({
                "type": "file",
                "path": str(dest),
                "displayName": filename,
            })
            print(f"[agent] File attachment ready: {filename} ({len(data)} bytes)", file=sys.stderr)

    return sdk_attachments


async def handle_user_message(
    state: AgentState,
    msg: Message,
) -> None:
    """Handle a user message — stream events back via IPC."""
    content = msg.payload.get("content", "")
    timestamp = msg.payload.get("timestamp", "")
    raw_attachments = msg.payload.get("attachments") or []

    # Track for the Mimir compaction-hook summary buffer.
    if state.mimir_enabled and content:
        try:
            state.mimir_recent_user_msgs.append((time.time(), content[:1000]))
        except Exception:
            pass

    # Prepend current time context so the agent knows when the message was sent
    if timestamp:
        content = f"<current_datetime>{timestamp}</current_datetime>\n\n{content}"

    if state.sdk_session is None:
        await state.ipc.send(Message(
            type=MessageType.AGENT_RESPONSE,
            payload={"content": f"[echo] {content}", "in_reply_to": msg.id},
            reply_to=msg.id,
        ))
        return

    # Download any media attachments from Matrix
    sdk_attachments = None
    if raw_attachments:
        try:
            sdk_attachments = await _download_attachments(state, raw_attachments)
            if not sdk_attachments:
                sdk_attachments = None
        except Exception as e:
            print(f"[agent] Attachment download failed: {e}", file=sys.stderr)
            sdk_attachments = None

    # Point the persistent listener at this message
    if state.listener_ctl and hasattr(state.listener_ctl, "set_current_msg"):
        state.listener_ctl.set_current_msg(msg.id)

    try:
        # Smart message injection: interrupt thinking, enqueue during tools
        priority = msg.payload.get("priority", False)
        if state.turn_active:
            if state.turn_phase == "thinking":
                print(f"[agent] Aborting thinking to inject message", file=sys.stderr)
                try:
                    await state.sdk_session.abort()
                except Exception as e:
                    print(f"[agent] Abort failed (non-fatal): {e}", file=sys.stderr)
                print(f"[agent] Sending to SDK: {content[:100]}...", file=sys.stderr)
                await state.sdk_session.send(content, attachments=sdk_attachments)
            elif priority:
                print(f"[agent] PRIORITY inject (turn in {state.turn_phase} phase): {content[:100]}...", file=sys.stderr)
                await state.sdk_session.send(content, attachments=sdk_attachments, mode="immediate")
            else:
                print(f"[agent] Queuing message for end of turn ({state.turn_phase} phase): {content[:100]}...", file=sys.stderr)
                state.queued_user_messages.append((content, sdk_attachments))
                # Schedule nudge so the agent gets a coffee-break notification
                state.pending_interrupt = True
                state.turns_since_enqueue = 0
                state.enqueue_time = time.monotonic()
                state.pending_messages.append(content)
        else:
            print(f"[agent] Sending to SDK: {content[:100]}...", file=sys.stderr)
            await state.sdk_session.send(content, attachments=sdk_attachments)
        print(f"[agent] SDK send() returned", file=sys.stderr)
        # Don't wait for SESSION_IDLE here — the persistent listener handles
        # all responses including those from background sub-agents.
    except Exception as e:
        err_str = str(e)
        print(f"[agent] SDK send() error: {e}", file=sys.stderr)

        # Recoverable errors: session lost, or SDK subprocess died
        _recoverable = (
            "Session not found" in err_str
            or "Broken pipe" in err_str
            or "BrokenPipeError" in type(e).__name__
            or isinstance(e, (BrokenPipeError, ConnectionError, OSError))
        )

        if _recoverable:
            recovered = await _recover_sdk_session(state)
            if recovered:
                # Notify user about the recovery
                await state.ipc.send(Message(
                    type=MessageType.AGENT_RESPONSE,
                    payload={
                        "content": (
                            "⚠️ *Session recovered from checkpoint — "
                            "some recent context may be summarized. "
                            "Continuing...*"
                        ),
                        "in_reply_to": msg.id,
                    },
                    reply_to=msg.id,
                ))
                # Point new listener at this message and retry
                if state.listener_ctl and hasattr(state.listener_ctl, "set_current_msg"):
                    state.listener_ctl.set_current_msg(msg.id)
                try:
                    await state.sdk_session.send(content, attachments=sdk_attachments)
                    return
                except Exception as retry_err:
                    print(f"[agent] Retry after recovery failed: {retry_err}", file=sys.stderr)

        await state.ipc.send(Message(
            type=MessageType.AGENT_RESPONSE,
            payload={"content": f"[error] {e}", "in_reply_to": msg.id},
            reply_to=msg.id,
        ))


async def _recover_sdk_session(state: AgentState) -> bool:
    """Recover from a lost SDK session.

    Strategy:
    1. Resume on the existing CopilotClient (reuses Node.js subprocess).
       The SDK loads session state from .copilot-state/ on disk, which
       includes conversation history via infinite_sessions checkpoints.
    2. If the existing client can't resume, do a full re-init (new
       subprocess + resume from disk).

    In both cases the event listener is re-registered on the new session.
    """
    old_client = state.sdk_client
    old_session_id = getattr(state.sdk_session, "session_id", None)
    print(f"[agent] Recovering SDK session (old={old_session_id})", file=sys.stderr)

    # Unsubscribe old event listener — will re-register on new session
    if state.listener_ctl and callable(state.listener_ctl):
        try:
            state.listener_ctl()
        except Exception:
            pass
        state.listener_ctl = None

    # Strategy 1: Resume on existing client (same Node.js subprocess)
    if old_client:
        try:
            result = await try_init_copilot(
                working_directory=state.working_directory,
                ipc=state.ipc,
                existing_client=old_client,
                agent_state=state,
            )
            if result:
                state.sdk_client, state.sdk_session = result
                state.listener_ctl = setup_session_listener(
                    state.ipc, state.sdk_session, state.loop, state,
                )
                print(
                    f"[agent] Recovered on existing client: "
                    f"{state.sdk_session.session_id}",
                    file=sys.stderr,
                )
                return True
        except Exception as e:
            print(f"[agent] Recovery on existing client failed: {e}", file=sys.stderr)

    # Strategy 2: Full re-init (new Node.js subprocess)
    print("[agent] Falling back to full SDK re-init", file=sys.stderr)
    if old_client:
        try:
            await old_client.stop()
        except Exception:
            pass

    try:
        result = await try_init_copilot(
            working_directory=state.working_directory,
            ipc=state.ipc,
            agent_state=state,
        )
        if result:
            state.sdk_client, state.sdk_session = result
            state.listener_ctl = setup_session_listener(
                state.ipc, state.sdk_session, state.loop, state,
            )
            print(
                f"[agent] Recovered with new client: "
                f"{state.sdk_session.session_id}",
                file=sys.stderr,
            )
            return True
    except Exception as e:
        print(f"[agent] Full SDK re-init failed: {e}", file=sys.stderr)

    return False


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
    try:
        from copilot import PermissionRequestResult
    except ImportError:
        from copilot.session import PermissionRequestResult

    if not ipc or not ipc.is_connected:
        return PermissionRequestResult(kind="reject")

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
                return PermissionRequestResult(kind="approve-once")
            return PermissionRequestResult(kind="reject")
        except Exception as exc:
            print(f"[agent] Permission request failed: {exc}", file=sys.stderr)
            return PermissionRequestResult(kind="reject")

    return _ask()  # Returns a coroutine (Awaitable) — SDK will await it


_MODEL_PREFERENCES: tuple[str, ...] = (
    "claude-opus-4.6",
    "claude-opus-4.7",
    "gpt-5.5",
)
_REASONING_EFFORT = "medium"

# Panel archetype model preferences (first available wins).
# Architect wants the deepest reasoning; Pragmatist & Contrarian favour
# GPT 5.5 for breadth; Skeptic uses Opus 4.6 for careful analysis.
_PANEL_MODEL_PREFERENCES: dict[str, tuple[str, ...]] = {
    "architect": ("claude-opus-4.7-xhigh", "claude-opus-4.6", "claude-opus-4.5"),
    "pragmatist": ("gpt-5.5", "gpt-5.4", "gpt-5.2"),
    "skeptic": ("claude-opus-4.6", "claude-opus-4.7", "claude-opus-4.5"),
    "contrarian": ("gpt-5.5", "claude-opus-4.7-xhigh", "claude-opus-4.6"),
}

# Populated at startup by _configure_model(); shared with consult_panel.
_AVAILABLE_MODEL_IDS: set[str] = set()


def _resolve_model(preferences: tuple[str, ...]) -> str:
    """Pick the first model from preferences that's actually available.

    Falls back to the first preference if the available-models set hasn't
    been populated yet (e.g. list_models failed at startup). The caller
    can then log and handle the case where the returned model is still
    unavailable.
    """
    if not _AVAILABLE_MODEL_IDS:
        return preferences[0]
    for candidate in preferences:
        if candidate in _AVAILABLE_MODEL_IDS:
            return candidate
    return preferences[0]


async def _configure_model(
    session: _CopilotSession, client: _CopilotClient | None = None,
) -> None:
    """Switch to the best available preferred model with reasoning enabled.

    Tries each model in _MODEL_PREFERENCES in order and uses the first one
    that's actually available for this session. Needed because set_model()
    silently falls back to a default model if the requested one is unavailable
    (e.g. claude-opus-4.7 not rolled out yet) — logging success but actually
    using a weaker model.
    """
    global _AVAILABLE_MODEL_IDS
    # Try to discover available models via the client.
    target = _MODEL_PREFERENCES[0]
    try:
        if client is not None:
            try:
                models = await client.list_models()
                _AVAILABLE_MODEL_IDS = {m.id for m in models}
            except Exception as e:
                # Newer CLI releases may return models with null `supports`/`limits`
                # fields that trip the SDK's pydantic parser. Fall back to a raw
                # JSON-RPC call and extract just the ids we need.
                print(
                    f"[agent] list_models parse failed ({e}); trying raw RPC",
                    file=sys.stderr,
                )
                raw = await client._client.request("models.list", {})
                _AVAILABLE_MODEL_IDS = {
                    m.get("id") for m in raw.get("models", []) if m.get("id")
                }
            chosen = next(
                (m for m in _MODEL_PREFERENCES if m in _AVAILABLE_MODEL_IDS),
                None,
            )
            if chosen is None:
                print(
                    f"[agent] WARNING: none of {_MODEL_PREFERENCES} are available. "
                    f"Available: {sorted(_AVAILABLE_MODEL_IDS)}",
                    file=sys.stderr,
                )
                return
            target = chosen
            if target != _MODEL_PREFERENCES[0]:
                print(
                    f"[agent] Preferred model {_MODEL_PREFERENCES[0]} unavailable; "
                    f"falling back to {target}",
                    file=sys.stderr,
                )
    except Exception as e:
        print(f"[agent] list_models failed (using {target}): {e}", file=sys.stderr)

    try:
        await session.set_model(target, reasoning_effort=_REASONING_EFFORT)
        print(
            f"[agent] Model set to {target} (reasoning={_REASONING_EFFORT})",
            file=sys.stderr,
        )
    except Exception as e:
        print(f"[agent] set_model failed (non-fatal): {e}", file=sys.stderr)

    # Write available models to workspace for webui consumption
    try:
        import json as _json
        models_info = {
            "current": target,
            "available": sorted(_AVAILABLE_MODEL_IDS) if _AVAILABLE_MODEL_IDS else [target],
            "preferences": list(_MODEL_PREFERENCES),
        }
        models_path = Path(os.environ.get("WORKSPACE_DIR", "/workspace")) / ".enclave-models.json"
        models_path.write_text(_json.dumps(models_info, indent=2))
        print(f"[agent] Wrote models info to {models_path}", file=sys.stderr)
    except Exception as e:
        print(f"[agent] Failed to write models info: {e}", file=sys.stderr)


async def try_init_copilot(
    working_directory: str = "/workspace",
    ipc: IPCClient | None = None,
    existing_client: _CopilotClient | None = None,
    agent_state: AgentState | None = None,
) -> tuple[_CopilotClient, _CopilotSession] | None:
    """Try to initialize the Copilot SDK.

    Attempts to resume the most recent session first (preserving conversation
    history across container restarts). Falls back to creating a new session.

    If *existing_client* is provided the Node.js subprocess is reused —
    this is the fast path for session recovery after idle timeout.

    Returns (client, session) tuple or None if SDK unavailable.
    """
    try:
        from copilot import CopilotClient, SubprocessConfig
        # Tool/ToolResult moved from copilot.types to copilot.tools in newer SDK
        try:
            from copilot.types import Tool, ToolResult
        except ImportError:
            from copilot.tools import Tool, ToolResult
        # PermissionRequestResult/SystemMessageAppendConfig moved to copilot.session
        try:
            from copilot import PermissionRequestResult, SystemMessageAppendConfig
        except ImportError:
            from copilot.session import PermissionRequestResult, SystemMessageAppendConfig
    except ImportError:
        return None

    try:
        if existing_client:
            # Reuse the Node.js subprocess — fast recovery path
            client = existing_client
        else:
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
            # ☕ Coffee break: nudge the agent if a user message is waiting.
            # SDK 0.3.0 dropped per-result feedback strings — we still reject
            # the tool call, and the queued user message will be delivered at
            # the next session.idle, so the agent learns why naturally.
            if (agent_state is not None
                    and agent_state.pending_interrupt
                    and agent_state.turns_since_enqueue >= AgentState.NUDGE_TURNS):
                return PermissionRequestResult(kind="reject")

            # Doom loop detection now sends a diagnostic message via
            # sdk_session.send(mode="immediate") in the TURN_START handler —
            # we intentionally don't also deny the next tool call here,
            # because a double-hit (message + denial) feels punitive and
            # blocks useful work while the agent is trying to self-assess.

            # Containers are already sandboxed — auto-approve everything
            if not is_host:
                return PermissionRequestResult(kind="approve-once")

            # YOLO mode: auto-approve all SDK tools
            if is_yolo:
                return PermissionRequestResult(kind="approve-once")

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
                return PermissionRequestResult(kind="approve-once")

            if kind == "read":
                path = getattr(_req, "path", "") or ""
                if not _is_in_scratch(path, working_directory):
                    reason = getattr(_req, "intention", "") or f"Read: {path}"
                    return _request_permission_sync(
                        ipc, "filesystem", path, reason,
                    )
                return PermissionRequestResult(kind="approve-once")

            if kind == "write":
                path = getattr(_req, "file_name", "") or ""
                if not _is_in_scratch(path, working_directory):
                    reason = getattr(_req, "intention", "") or f"Write: {path}"
                    return _request_permission_sync(
                        ipc, "filesystem", path, reason,
                    )
                return PermissionRequestResult(kind="approve-once")

            # url, mcp, memory, hook, custom-tool — auto-approve
            return PermissionRequestResult(kind="approve-once")

        prompt_dir = Path(__file__).parent / "prompts"
        prompt_parts = []
        for filename in ("base.md", "guidelines.md", f"{profile_name}.md"):
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

        # Load workspace-level instructions (.github/copilot-instructions.md)
        instructions_path = Path(working_directory) / ".github" / "copilot-instructions.md"
        if instructions_path.exists():
            instructions_text = instructions_path.read_text().strip()
            if instructions_text:
                prompt_parts.append(
                    "# Workspace Instructions\n\n"
                    "The following instructions were provided by the project owner. "
                    "Follow them unless they conflict with core safety guidelines.\n\n"
                    + instructions_text
                )
                print(f"[agent] Loaded workspace instructions ({len(instructions_text)} chars)", file=sys.stderr)

        # Load session-specific prompt (.enclave-session-prompt)
        session_prompt_path = Path(working_directory) / ".enclave-session-prompt"
        if session_prompt_path.exists():
            session_prompt_text = session_prompt_path.read_text().strip()
            if session_prompt_text:
                prompt_parts.append(
                    "# Session-Specific Instructions\n\n"
                    "The following instructions are specific to this session. "
                    "Follow them unless they conflict with core safety guidelines.\n\n"
                    + session_prompt_text
                )
                print(f"[agent] Loaded session prompt ({len(session_prompt_text)} chars)", file=sys.stderr)

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
                "The user must approve via a poll. Once approved, the container "
                "will RESTART to apply the mount — your session state is preserved "
                "and you will resume automatically. The mounted path appears read-only "
                "at /workspace/<mount-name>. "
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
                "Launch a GUI application on the user's desktop. This runs the "
                "command on the HOST machine (not inside the container), so it has "
                "access to the user's Wayland/X11 display. Requires user approval. "
                "Use for browsers, editors, image viewers, media players, etc. "
                "Example: launch_gui(command='firefox https://example.com', reason='Open docs'). "
                "To open a file from the workspace, use the full path under /workspace/."
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
        if not (agent_state and agent_state.mimir_enabled):
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

        # ── System Status Tool ──
        async def _system_status_handler(params: dict) -> str:
            """Gather host system information."""
            import shutil
            sections = []

            # Uptime
            try:
                proc = await asyncio.create_subprocess_exec(
                    "uptime", "-p",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
                sections.append(f"**Uptime:** {stdout.decode().strip()}")
            except Exception:
                sections.append("**Uptime:** unavailable")

            # CPU info
            try:
                proc = await asyncio.create_subprocess_exec(
                    "nproc",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
                cpus = stdout.decode().strip()

                proc2 = await asyncio.create_subprocess_exec(
                    "cat", "/proc/loadavg",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout2, _ = await asyncio.wait_for(proc2.communicate(), timeout=5)
                load = stdout2.decode().strip().split()[:3]
                sections.append(f"**CPU:** {cpus} cores, load avg: {' '.join(load)}")
            except Exception:
                sections.append("**CPU:** unavailable")

            # Memory
            try:
                proc = await asyncio.create_subprocess_exec(
                    "free", "-h", "--si",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
                lines = stdout.decode().strip().split("\n")
                if len(lines) >= 2:
                    parts = lines[1].split()
                    if len(parts) >= 3:
                        sections.append(f"**Memory:** {parts[2]} used / {parts[1]} total")
            except Exception:
                sections.append("**Memory:** unavailable")

            # Disk
            try:
                total, used, free = shutil.disk_usage("/")
                total_gb = total / (1024**3)
                used_gb = used / (1024**3)
                free_gb = free / (1024**3)
                pct = (used / total) * 100
                sections.append(
                    f"**Disk (/):** {used_gb:.1f}G used / {total_gb:.1f}G total "
                    f"({pct:.0f}%), {free_gb:.1f}G free"
                )
            except Exception:
                sections.append("**Disk:** unavailable")

            # Network (basic connectivity)
            try:
                proc = await asyncio.create_subprocess_exec(
                    "hostname", "-I",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
                ips = stdout.decode().strip().split()[:3]
                sections.append(f"**Network:** {', '.join(ips)}")
            except Exception:
                sections.append("**Network:** unavailable")

            # Pending system updates (if apt available)
            try:
                proc = await asyncio.create_subprocess_exec(
                    "bash", "-c",
                    "apt list --upgradable 2>/dev/null | grep -c upgradable || echo 0",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
                count = stdout.decode().strip()
                if count and count != "0":
                    sections.append(f"**Updates:** {count} packages upgradable")
                else:
                    sections.append("**Updates:** system up to date")
            except Exception:
                pass

            # Systemd failed units
            try:
                proc = await asyncio.create_subprocess_exec(
                    "systemctl", "--user", "--failed", "--no-legend", "--no-pager",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
                failed = stdout.decode().strip()
                if failed:
                    sections.append(f"**Failed services:**\n```\n{failed}\n```")
                else:
                    sections.append("**Services:** all healthy")
            except Exception:
                pass

            return "\n".join(sections)

        system_status_tool = Tool(
            name="system_status",
            description=(
                "Get a comprehensive system status report including uptime, CPU, "
                "memory, disk, network, pending updates, and service health."
            ),
            handler=_system_status_handler,
            parameters={
                "type": "object",
                "properties": {},
            },
        )
        custom_tools.append(system_status_tool)

        # Custom tool: check_messages — peek at pending user messages
        async def _check_messages_handler(_invocation: object) -> ToolResult:
            if not agent_state.pending_messages:
                return ToolResult(
                    text_result_for_llm="No pending messages.",
                )
            preview = "\n---\n".join(agent_state.pending_messages)
            return ToolResult(
                text_result_for_llm=(
                    f"📬 {len(agent_state.pending_messages)} pending message(s):\n\n"
                    f"{preview}\n\n"
                    "The full message will be delivered when your current task "
                    "completes. Please wrap up your current step."
                ),
            )

        check_messages_tool = Tool(
            name="check_messages",
            description=(
                "Check if the user has sent any messages while you were working. "
                "Call this if a tool call is denied with a 'message waiting' notice, "
                "or at natural breakpoints in long tasks."
            ),
            handler=_check_messages_handler,
            parameters={
                "type": "object",
                "properties": {},
            },
            skip_permission=True,
        )
        custom_tools.append(check_messages_tool)

        # Custom tool: mark_done — signal that the agent has finished its work
        async def _mark_done_handler(invocation: object) -> ToolResult:
            args = getattr(invocation, "arguments", {}) or {}
            summary = args.get("summary", "")
            if agent_state is not None:
                agent_state.task_done = True
            if ipc and ipc.is_connected:
                await ipc.send(Message(
                    type=MessageType.TASK_DONE,
                    payload={"summary": summary},
                ))
            return ToolResult(
                text_result_for_llm=(
                    "Marked as done. The session will stay idle until the user "
                    "sends a new message."
                ),
            )

        mark_done_tool = Tool(
            name="mark_done",
            description=(
                "Signal that you have finished your current work and are waiting "
                "for the user. Call this when you've completed all items on your "
                "plan, or when there's nothing more to do without user input. "
                "If you don't call this, the framework will ask you to continue."
            ),
            handler=_mark_done_handler,
            parameters={
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "Brief summary of what was accomplished.",
                    },
                },
            },
            skip_permission=True,
        )
        custom_tools.append(mark_done_tool)

        # Custom tool: ask_user — ask the user a question (optionally as a poll)
        async def _ask_user_handler(invocation: object) -> ToolResult:
            args = getattr(invocation, "arguments", {}) or {}
            question = args.get("question", "").strip()
            choices = args.get("choices") or []
            if not question:
                return ToolResult(
                    text_result_for_llm="Error: 'question' parameter is required.",
                    result_type="error",
                )
            if agent_state is not None:
                agent_state.asked_user = True
            if ipc and ipc.is_connected:
                await ipc.send(Message(
                    type=MessageType.ASK_USER,
                    payload={
                        "question": question,
                        "choices": choices if choices else None,
                    },
                ))
            return ToolResult(
                text_result_for_llm=(
                    "Question sent to the user. The session will stay idle "
                    "until they respond. Their reply will arrive as a normal "
                    "message."
                ),
            )

        ask_user_tool = Tool(
            name="ask_user",
            description=(
                "Ask the user a question and wait for their response. Use this "
                "when you need a decision, clarification, or want to know what "
                "the user wants to work on next. Optionally provide choices for "
                "a poll-style question."
            ),
            handler=_ask_user_handler,
            parameters={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The question to ask the user.",
                    },
                    "choices": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional list of choices for a poll-style question. "
                            "If omitted, the question is sent as plain text."
                        ),
                    },
                },
                "required": ["question"],
            },
            skip_permission=True,
        )
        custom_tools.append(ask_user_tool)

        # Custom tool: enter_nix_shell — restart under a nix-shell environment
        async def _enter_nix_shell_handler(invocation: object) -> ToolResult:
            args = getattr(invocation, "arguments", {}) or {}
            nix_path = args.get("path", "")
            if not nix_path:
                return ToolResult(
                    text_result_for_llm="Error: 'path' parameter is required (e.g. /workspace/shell.nix)",
                    result_type="error",
                )
            if not ipc or not ipc.is_connected:
                return ToolResult(
                    text_result_for_llm="Error: not connected to orchestrator",
                    result_type="error",
                )
            # Check if already running under this nix-shell
            current = os.environ.get("ENCLAVE_NIX_SHELL", "")
            if current == nix_path and not os.environ.get("ENCLAVE_NIX_SHELL_FAILED"):
                return ToolResult(
                    text_result_for_llm=f"Already running under nix-shell {nix_path}",
                )
            await ipc.send(Message(
                type=MessageType.NIX_SHELL_REQUEST,
                payload={"path": nix_path},
            ))
            return ToolResult(
                text_result_for_llm=(
                    f"Nix-shell switch to {nix_path} requested. "
                    "The session will restart momentarily and resume from checkpoint. "
                    "All subsequent commands will run in the nix-shell environment."
                ),
            )

        nix_shell_tool = Tool(
            name="enter_nix_shell",
            description=(
                "Restart the session under a nix-shell environment. "
                "This makes all nix packages from the specified shell.nix or "
                "default.nix available for subsequent commands without needing "
                "to prefix them with 'nix-shell --run'. The session restarts "
                "but conversation history is preserved via checkpoints. "
                "Use this when you need to repeatedly use tools from a nix expression."
            ),
            handler=_enter_nix_shell_handler,
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the nix file (e.g. /workspace/shell.nix, /workspace/default.nix)",
                    },
                },
                "required": ["path"],
            },
        )
        custom_tools.append(nix_shell_tool)

        # Custom tool: request_port — request a host port mapping for this container
        async def _request_port_handler(invocation: object) -> ToolResult:
            args = getattr(invocation, "arguments", {}) or {}
            container_port = args.get("container_port")
            protocol = args.get("protocol", "tcp").lower()

            if not container_port or not isinstance(container_port, int):
                return ToolResult(
                    text_result_for_llm="Error: 'container_port' must be an integer (1-65535)",
                    result_type="error",
                )
            if protocol not in ("tcp", "udp"):
                return ToolResult(
                    text_result_for_llm="Error: 'protocol' must be 'tcp' or 'udp'",
                    result_type="error",
                )
            if not ipc or not ipc.is_connected:
                return ToolResult(
                    text_result_for_llm="Error: not connected to orchestrator",
                    result_type="error",
                )

            try:
                reply = await ipc.request(Message(
                    type=MessageType.PORT_REQUEST,
                    payload={
                        "container_port": container_port,
                        "protocol": protocol,
                    },
                ), timeout=15.0)
            except asyncio.TimeoutError:
                return ToolResult(
                    text_result_for_llm="Error: port request timed out",
                    result_type="error",
                )

            payload = reply.payload
            if "error" in payload:
                return ToolResult(
                    text_result_for_llm=f"Error: {payload['error']}",
                    result_type="error",
                )

            host_port = payload["host_port"]
            hostname = payload["hostname"]
            active = payload.get("active", False)
            restart_required = payload.get("restart_required", False)
            already_existed = payload.get("already_existed", False)

            lines = [
                f"Port mapping {'already exists' if already_existed else 'created'}:",
                f"  Container port: {container_port}/{protocol}",
                f"  Accessible at:  {hostname}:{host_port}",
                f"  Status:         {'Active' if active else 'Inactive (restart required)'}",
            ]
            if restart_required:
                lines.append("")
                lines.append("⚠️ The session must be restarted for this mapping to take effect.")
            if not active:
                lines.append("")
                lines.append(
                    f"IMPORTANT: When starting your service, bind to 0.0.0.0:{container_port} "
                    f"(not 127.0.0.1) inside the container for the port to be reachable."
                )
            lines.append("")
            lines.append(f"Tell the user to access the service at: {hostname}:{host_port}")

            return ToolResult(text_result_for_llm="\n".join(lines))

        request_port_tool = Tool(
            name="request_port",
            description=(
                "Request a host port mapping for this container session. "
                "Maps a container port to a host port so users can access "
                "services running inside the container (e.g., dev servers, "
                "web apps, game servers). The mapping is permanent and "
                "persists across restarts. A session restart is required "
                "to activate new mappings. Use this when you need to expose "
                "a service for the user to connect to."
            ),
            handler=_request_port_handler,
            parameters={
                "type": "object",
                "properties": {
                    "container_port": {
                        "type": "integer",
                        "description": "Port number inside the container (1-65535)",
                    },
                    "protocol": {
                        "type": "string",
                        "enum": ["tcp", "udp"],
                        "description": "Protocol: 'tcp' (default) or 'udp'",
                    },
                },
                "required": ["container_port"],
            },
        )
        custom_tools.append(request_port_tool)

        # ── Mimir recall tool ────────────────────────────────────────
        # Spawns mimir-cli per call (no persistent process) and substring-
        # greps the decoded canonical log. v1 uses substring match because
        # mimir-mcp's semantic recall requires Lisp `(query ...)` forms
        # which we don't construct yet. Skipped silently when Mimir is
        # disabled or the killswitch has tripped.
        async def _mimir_recall_handler(invocation: object) -> ToolResult:
            if not agent_state or not agent_state.mimir_enabled:
                return ToolResult(
                    text_result_for_llm=(
                        "Mimir recall is disabled for this session. "
                        "Skipping silently."
                    ),
                )
            if agent_state.mimir_killswitch_reason:
                return ToolResult(
                    text_result_for_llm=(
                        f"Mimir recall is disabled (killswitch tripped: "
                        f"{agent_state.mimir_killswitch_reason})."
                    ),
                )
            args = getattr(invocation, "arguments", {}) or {}
            query = (args.get("query") or "").strip()
            limit = int(args.get("limit") or 5)
            if not query:
                return ToolResult(
                    text_result_for_llm="Error: 'query' is required.",
                    result_type="error",
                )
            limit = max(1, min(limit, 50))
            log_path = f"{agent_state.mimir_workspace}/canonical.log"
            cli_bin = agent_state.mimir_cli_bin
            if not Path(log_path).exists():
                return ToolResult(
                    text_result_for_llm=(
                        f"No prior memories — canonical log not yet "
                        f"created at {log_path}."
                    ),
                )
            try:
                proc = await asyncio.create_subprocess_exec(
                    cli_bin, "decode", log_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=15.0,
                )
                if proc.returncode != 0:
                    raise RuntimeError(
                        f"mimir-cli decode rc={proc.returncode}: "
                        f"{stderr.decode('utf-8', errors='replace')[:200]}"
                    )
            except Exception as e:
                agent_state.mimir_failure_count += 1
                # Trip the killswitch after 3 consecutive failures so
                # repeated mimir errors can never spiral into a doom loop.
                if agent_state.mimir_failure_count >= 3:
                    agent_state.mimir_killswitch_reason = (
                        f"3 consecutive mimir-cli failures; last: {e}"
                    )
                    print(
                        f"[agent] Mimir killswitch tripped: "
                        f"{agent_state.mimir_killswitch_reason}",
                        file=sys.stderr,
                    )
                return ToolResult(
                    text_result_for_llm=f"Mimir recall failed: {e}",
                    result_type="error",
                )
            # Reset counter on success.
            agent_state.mimir_failure_count = 0
            text = stdout.decode("utf-8", errors="replace")
            terms = [t for t in re.split(r"\W+", query.lower()) if len(t) >= 3]
            if not terms:
                terms = [query.lower()]
            matches: list[str] = []
            for line in text.splitlines():
                lower = line.lower()
                if any(term in lower for term in terms):
                    matches.append(line)
                    if len(matches) >= limit:
                        break
            if not matches:
                return ToolResult(
                    text_result_for_llm=(
                        f"No prior memories matched terms: {terms}. "
                        f"Canonical log has {text.count(chr(10))} records."
                    ),
                )
            body = "\n".join(matches)
            return ToolResult(
                text_result_for_llm=(
                    f"Recalled {len(matches)} record(s) matching {terms}:\n"
                    f"{body}"
                ),
            )

        mimir_recall_tool = Tool(
            name="mimir_recall",
            description=(
                "Recall durable cross-session memories from Mimir before "
                "reasoning about a problem. Use this when the user reports "
                "a bug, regression, or unexpected behaviour, or when you "
                "need historical context about the project (e.g. 'what "
                "did we try last time virtio-net crashed?'). Returns "
                "matching canonical-log records. Read-only — does not "
                "modify any memory. Silently no-ops if Mimir is disabled."
            ),
            handler=_mimir_recall_handler,
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Free-text query. Whitespace/punctuation is split "
                            "into terms; records matching any term are returned."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max records to return (1–50, default 5).",
                    },
                },
                "required": ["query"],
            },
            skip_permission=True,
        )
        custom_tools.append(mimir_recall_tool)

        # ── Mimir record tool ────────────────────────────────────────
        # Shells out to `mimir-librarian submit` per call (slow LLM-side
        # processing happens later, asynchronously, when the host sweeper
        # runs). The sync submit is just file I/O — it writes a v2 envelope
        # to drafts/pending/. Skipped silently when Mimir is disabled or
        # the killswitch has tripped.
        async def _mimir_submit_draft(
            *,
            prose: str,
            durability: str,
            tags: list[str] | None = None,
            source_surface: str = "agent-export",
        ) -> tuple[bool, str]:
            """Submit one prose draft to the librarian. Returns (ok, message).

            Shared between the mimir_record tool and the compaction hook.
            Caller is responsible for guarding on mimir_enabled / killswitch.
            """
            if not agent_state:
                return (False, "agent_state unavailable")
            librarian_bin = agent_state.mimir_librarian_bin
            drafts_dir = f"{agent_state.mimir_workspace}/drafts"
            agent_name = os.environ.get("ENCLAVE_MIMIR_AGENT_NAME", "brook")
            project_name = os.environ.get("ENCLAVE_USER_NAME", "") or "enclave"
            operator = os.environ.get("ENCLAVE_USER_NAME", "ian")
            # Durability cue — librarian's classifier weights @observation
            # / @policy as 1.0, @agent_instruction as 0.95, @self_report 0.9.
            # Map our coarse durability flag to the right marker.
            marker = {
                "permanent": "@observation",
                "policy": "@policy",
                "instruction": "@agent_instruction",
                "transient": "@self_report",
            }.get(durability, "@self_report")
            # Always include the marker prefix; the librarian peels it off
            # and uses it for confidence scoring.
            framed = f"{marker}\n\n{prose.strip()}"
            argv = [
                librarian_bin, "submit",
                "--text", framed,
                "--drafts-dir", drafts_dir,
                "--source-surface", source_surface,
                "--agent", agent_name,
                "--project", project_name,
                "--operator", operator,
            ]
            for tag in tags or []:
                argv.extend(["--tag", tag])
            try:
                Path(drafts_dir).mkdir(parents=True, exist_ok=True)
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=10.0,
                )
                if proc.returncode != 0:
                    err = stderr.decode("utf-8", errors="replace")[:200]
                    raise RuntimeError(f"librarian submit rc={proc.returncode}: {err}")
                # Reset on success.
                agent_state.mimir_failure_count = 0
                return (True, stdout.decode("utf-8", errors="replace").strip())
            except Exception as e:
                agent_state.mimir_failure_count += 1
                if agent_state.mimir_failure_count >= 3:
                    agent_state.mimir_killswitch_reason = (
                        f"3 consecutive mimir-librarian failures; last: {e}"
                    )
                    print(
                        f"[agent] Mimir killswitch tripped: "
                        f"{agent_state.mimir_killswitch_reason}",
                        file=sys.stderr,
                    )
                return (False, f"submit failed: {e}")

        # Expose to the agent_state so the compaction hook can reuse it.
        # Avoids duplicating the validation/killswitch logic.
        if agent_state is not None:
            agent_state._mimir_submit_draft = _mimir_submit_draft  # type: ignore[attr-defined]

        async def _mimir_record_handler(invocation: object) -> ToolResult:
            if not agent_state or not agent_state.mimir_enabled:
                return ToolResult(
                    text_result_for_llm=(
                        "Mimir record is disabled for this session. "
                        "Skipping silently."
                    ),
                )
            if agent_state.mimir_killswitch_reason:
                return ToolResult(
                    text_result_for_llm=(
                        f"Mimir record is disabled (killswitch tripped: "
                        f"{agent_state.mimir_killswitch_reason})."
                    ),
                )
            args = getattr(invocation, "arguments", {}) or {}
            prose = (args.get("prose") or "").strip()
            durability = (args.get("durability") or "permanent").strip().lower()
            extra_tags_raw = args.get("tags") or []
            if not prose:
                return ToolResult(
                    text_result_for_llm="Error: 'prose' is required.",
                    result_type="error",
                )
            if durability not in {"permanent", "policy", "instruction", "transient"}:
                return ToolResult(
                    text_result_for_llm=(
                        "Error: 'durability' must be one of: "
                        "permanent, policy, instruction, transient."
                    ),
                    result_type="error",
                )
            if len(prose) > 8000:
                return ToolResult(
                    text_result_for_llm=(
                        f"Error: prose too long ({len(prose)} chars; max 8000). "
                        "Split into multiple records or summarise first."
                    ),
                    result_type="error",
                )
            extra_tags = [
                str(t).strip()[:64]
                for t in extra_tags_raw
                if isinstance(t, str) and t.strip()
            ][:8]
            ok, msg = await _mimir_submit_draft(
                prose=prose,
                durability=durability,
                tags=["agent-tool:mimir_record", *extra_tags],
            )
            if not ok:
                return ToolResult(
                    text_result_for_llm=f"Mimir record failed: {msg}",
                    result_type="error",
                )
            return ToolResult(
                text_result_for_llm=(
                    f"Recorded ({durability}). Draft will be processed "
                    f"asynchronously by the librarian sweeper.\n{msg}"
                ),
            )

        mimir_record_tool = Tool(
            name="mimir_record",
            description=(
                "Record a durable cross-session memory in Mimir. Use this "
                "AFTER you have confirmed something important with the user "
                "and want it preserved across compactions and future "
                "sessions: a hard-won lesson, an architectural fact, a "
                "policy the operator has confirmed, or a notable milestone. "
                "Do NOT use this for routine status updates or transient "
                "scratch notes. The draft is processed asynchronously by a "
                "host-side sweeper — the canonical log is not updated "
                "synchronously. Silently no-ops if Mimir is disabled."
            ),
            handler=_mimir_record_handler,
            parameters={
                "type": "object",
                "properties": {
                    "prose": {
                        "type": "string",
                        "description": (
                            "Plain English description of the fact, lesson, or "
                            "event to record. State the subject explicitly (e.g. "
                            "'Brook orchestrator', 'Khione host system') rather "
                            "than relying on context. Include dates for events. "
                            "Max 8000 characters."
                        ),
                    },
                    "durability": {
                        "type": "string",
                        "enum": ["permanent", "policy", "instruction", "transient"],
                        "description": (
                            "How the librarian should weight this record. "
                            "'permanent' = witnessed historical fact (default). "
                            "'policy' = enduring rule (condition → action). "
                            "'instruction' = agent intent / TODO. "
                            "'transient' = ephemeral observation."
                        ),
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional short tags (max 8, each ≤64 chars) for "
                            "later filtering. Avoid PII or secrets."
                        ),
                    },
                },
                "required": ["prose"],
            },
            skip_permission=True,
        )
        custom_tools.append(mimir_record_tool)

        # ── Bug tracker tools (workspace-local source of truth, mirrored to
        # Mimir for cross-session visibility). Tracking IDs are per-project,
        # derived from SESSION_NAME, e.g. "Memory Test 4" → MEM-001.
        from enclave.agent import bug_tracker as _bugs

        _bug_workspace = working_directory
        _session_name = os.environ.get("SESSION_NAME", "") or os.environ.get(
            "ENCLAVE_PROJECT_NAME", ""
        )
        _bug_prefix = _bugs.compute_prefix(_session_name)

        def _mirror_bug_to_mimir(bug, action: str, extra: str = "") -> None:
            """Fire-and-forget Mimir record. No-op when Mimir is off."""
            if not agent_state or not getattr(agent_state, "mimir_enabled", False):
                return
            if getattr(agent_state, "mimir_killswitch_reason", None):
                return
            submit = getattr(agent_state, "_mimir_submit_draft", None)
            if not submit:
                return
            prose_parts = [
                f"Bug {bug.id} {action}: {bug.title}.",
                f"Status: {bug.status}, severity: {bug.severity}.",
            ]
            if extra:
                prose_parts.append(extra.strip())
            prose = " ".join(prose_parts)
            asyncio.create_task(
                submit(
                    prose=prose,
                    durability="permanent",
                    tags=[f"bug:{bug.id}", f"bug-status:{bug.status}", "bug-tracker"],
                    source_surface="agent-tool:bug_tracker",
                )
            )

        async def _bug_open_handler(invocation: object) -> ToolResult:
            args = getattr(invocation, "arguments", {}) or {}
            title = (args.get("title") or "").strip()
            description = (args.get("description") or "").strip()
            repro = (args.get("repro") or "").strip()
            severity = (args.get("severity") or "medium").strip().lower()
            if not title:
                return ToolResult(
                    text_result_for_llm="Error: 'title' is required",
                    result_type="error",
                )
            if not description:
                return ToolResult(
                    text_result_for_llm="Error: 'description' is required",
                    result_type="error",
                )
            try:
                bug = _bugs.open_bug(
                    _bug_workspace,
                    prefix=_bug_prefix,
                    title=title,
                    description=description,
                    repro=repro,
                    severity=severity,
                )
            except Exception as e:
                return ToolResult(
                    text_result_for_llm=f"Failed to open bug: {e}",
                    result_type="error",
                )
            _mirror_bug_to_mimir(bug, "opened", description[:200])
            return ToolResult(
                text_result_for_llm=(
                    f"Opened {bug.id} (severity={bug.severity}). "
                    f"File: .enclave-bugs/{bug.id}.md"
                ),
            )

        bug_open_tool = Tool(
            name="bug_open",
            description=(
                "Open a new bug with a tracking ID (e.g. MEM-001). Use the "
                "moment a bug is discovered — even if you'll fix it "
                "immediately. Captures title, description, repro steps, "
                "and severity. Returns the assigned ID. Bugs are stored at "
                ".enclave-bugs/<ID>.md and mirrored to Mimir."
            ),
            handler=_bug_open_handler,
            parameters={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Short one-line bug title",
                    },
                    "description": {
                        "type": "string",
                        "description": (
                            "What's wrong, observed vs expected behaviour, "
                            "any error messages or stack traces. Be specific."
                        ),
                    },
                    "repro": {
                        "type": "string",
                        "description": (
                            "Reproduction steps if known. Optional but "
                            "highly recommended."
                        ),
                    },
                    "severity": {
                        "type": "string",
                        "enum": list(_bugs.VALID_SEVERITY),
                        "description": "Severity (default: medium)",
                    },
                },
                "required": ["title", "description"],
            },
            skip_permission=True,
        )
        custom_tools.append(bug_open_tool)

        async def _bug_update_handler(invocation: object) -> ToolResult:
            args = getattr(invocation, "arguments", {}) or {}
            bug_id = (args.get("bug_id") or "").strip()
            status = (args.get("status") or "").strip().lower() or None
            severity = (args.get("severity") or "").strip().lower() or None
            note = (args.get("note") or "").strip()
            if not bug_id:
                return ToolResult(
                    text_result_for_llm="Error: 'bug_id' is required",
                    result_type="error",
                )
            if status and status not in _bugs.VALID_STATUS:
                return ToolResult(
                    text_result_for_llm=(
                        f"Invalid status '{status}'. "
                        f"Valid: {', '.join(_bugs.VALID_STATUS)}"
                    ),
                    result_type="error",
                )
            bug = _bugs.update_bug(
                _bug_workspace, bug_id,
                status=status, severity=severity, note=note,
            )
            if not bug:
                return ToolResult(
                    text_result_for_llm=f"Bug {bug_id} not found",
                    result_type="error",
                )
            action = f"updated → {bug.status}" if status else "updated"
            _mirror_bug_to_mimir(bug, action, note)
            return ToolResult(
                text_result_for_llm=(
                    f"Updated {bug.id} (status={bug.status}, "
                    f"severity={bug.severity})."
                ),
            )

        bug_update_tool = Tool(
            name="bug_update",
            description=(
                "Update an existing bug — change status, severity, or "
                "append a progress note. Use status='resolved' or "
                "'wontfix' to close. Use 'in_progress' when actively "
                "working on the fix, 'blocked' when stuck."
            ),
            handler=_bug_update_handler,
            parameters={
                "type": "object",
                "properties": {
                    "bug_id": {
                        "type": "string",
                        "description": "The bug ID, e.g. MEM-001",
                    },
                    "status": {
                        "type": "string",
                        "enum": list(_bugs.VALID_STATUS),
                        "description": "New status",
                    },
                    "severity": {
                        "type": "string",
                        "enum": list(_bugs.VALID_SEVERITY),
                        "description": "New severity",
                    },
                    "note": {
                        "type": "string",
                        "description": "Progress note appended to History",
                    },
                },
                "required": ["bug_id"],
            },
            skip_permission=True,
        )
        custom_tools.append(bug_update_tool)

        async def _bug_list_handler(invocation: object) -> ToolResult:
            args = getattr(invocation, "arguments", {}) or {}
            status_filter = (args.get("status") or "").strip().lower() or None
            if status_filter and status_filter not in _bugs.VALID_STATUS:
                return ToolResult(
                    text_result_for_llm=(
                        f"Invalid status filter '{status_filter}'. "
                        f"Valid: {', '.join(_bugs.VALID_STATUS)}"
                    ),
                    result_type="error",
                )
            bugs = _bugs.list_bugs(_bug_workspace, status_filter=status_filter)
            header = (
                f"Project bugs (prefix={_bug_prefix}, total={len(bugs)})"
            )
            if status_filter:
                header += f" [filter: status={status_filter}]"
            return ToolResult(
                text_result_for_llm=f"{header}\n\n{_bugs.render_table(bugs)}",
            )

        bug_list_tool = Tool(
            name="bug_list",
            description=(
                "List bugs for this project. Optionally filter by status. "
                "Use this BEFORE opening a new bug to check whether the "
                "issue is already tracked."
            ),
            handler=_bug_list_handler,
            parameters={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": list(_bugs.VALID_STATUS),
                        "description": "Filter to a specific status",
                    },
                },
            },
            skip_permission=True,
        )
        custom_tools.append(bug_list_tool)

        async def _bug_get_handler(invocation: object) -> ToolResult:
            args = getattr(invocation, "arguments", {}) or {}
            bug_id = (args.get("bug_id") or "").strip()
            if not bug_id:
                return ToolResult(
                    text_result_for_llm="Error: 'bug_id' is required",
                    result_type="error",
                )
            p = _bugs.bug_dir(_bug_workspace) / f"{bug_id}.md"
            if not p.is_file():
                return ToolResult(
                    text_result_for_llm=f"Bug {bug_id} not found",
                    result_type="error",
                )
            return ToolResult(text_result_for_llm=p.read_text())

        bug_get_tool = Tool(
            name="bug_get",
            description=(
                "Read the full markdown for a bug, including description, "
                "repro, and history. Use when iterating on a known bug."
            ),
            handler=_bug_get_handler,
            parameters={
                "type": "object",
                "properties": {
                    "bug_id": {
                        "type": "string",
                        "description": "The bug ID, e.g. MEM-001",
                    },
                },
                "required": ["bug_id"],
            },
            skip_permission=True,
        )
        custom_tools.append(bug_get_tool)

        # Custom tool: publish_artifact — create/update a versioned artifact document
        async def _publish_artifact_handler(invocation: object) -> ToolResult:
            args = getattr(invocation, "arguments", {}) or {}
            title = args.get("title", "").strip()
            filename = args.get("filename", "").strip()
            content = args.get("content", "")
            description = args.get("description", "")

            if not title or not filename or not content:
                return ToolResult(
                    text_result_for_llm="Error: 'title', 'filename', and 'content' are required",
                    result_type="error",
                )

            # Security: prevent path traversal
            if "/" in filename or "\\" in filename or filename.startswith("."):
                return ToolResult(
                    text_result_for_llm="Error: filename must be a simple name (no paths, no leading dot)",
                    result_type="error",
                )

            import json as _json
            art_dir = Path(working_directory) / "artifacts"
            art_dir.mkdir(parents=True, exist_ok=True)
            target = art_dir / filename
            manifest_path = Path(working_directory) / ".enclave-artifacts.json"

            # Load manifest
            entries: list[dict] = []
            if manifest_path.exists():
                try:
                    entries = _json.loads(manifest_path.read_text())
                except Exception:
                    entries = []

            from datetime import datetime as _dt, timezone as _tz
            now = _dt.now(_tz.utc).isoformat()

            # Check if this is an update (file already exists)
            existing = next((e for e in entries if e["filename"] == filename), None)
            if existing and target.exists():
                # Version the old file
                version = existing.get("version", 1)
                old_size = target.stat().st_size  # actual file size, not manifest cache
                stem = target.stem
                ext = target.suffix
                versioned_name = f"{stem}.v{version}{ext}"
                versioned_path = art_dir / versioned_name
                # Copy current to versioned
                import shutil
                shutil.copy2(str(target), str(versioned_path))
                # Record version in history
                versions = existing.get("versions", [])
                if not versions:
                    # Retroactively create v1 entry for the original version
                    versions.append({
                        "version": 1,
                        "created": existing.get("created", now),
                        "size": old_size if version == 1 else existing.get("size", 0),
                    })
                if version > 1 or not versions:
                    versions.append({
                        "version": version,
                        "created": existing.get("updated", now),
                        "size": old_size,
                    })
                # Update entry
                existing["version"] = version + 1
                existing["versions"] = versions
                existing["title"] = title
                existing["description"] = description
                existing["updated"] = now
                existing["size"] = len(content.encode("utf-8"))
            elif existing:
                # Entry exists but file doesn't (shouldn't happen, but handle gracefully)
                existing["title"] = title
                existing["description"] = description
                existing["version"] = existing.get("version", 1)
                existing["updated"] = now
                existing["size"] = len(content.encode("utf-8"))
            else:
                # New artifact
                entries.append({
                    "title": title,
                    "description": description,
                    "filename": filename,
                    "content_type": "text/markdown" if filename.endswith(".md") else "text/plain",
                    "size": len(content.encode("utf-8")),
                    "version": 1,
                    "versions": [],
                    "created": now,
                    "updated": now,
                })

            # Write the file
            target.write_text(content, encoding="utf-8")

            # Write manifest
            manifest_path.write_text(_json.dumps(entries, indent=2))

            version_num = next((e["version"] for e in entries if e["filename"] == filename), 1)
            return ToolResult(
                text_result_for_llm=(
                    f"Artifact published: {title} (v{version_num})\n"
                    f"File: artifacts/{filename}\n"
                    f"Link for user: [📎 {title}](/artifacts) — visible in the Artifacts panel"
                ),
            )

        publish_artifact_tool = Tool(
            name="publish_artifact",
            description=(
                "Create or update a versioned artifact document (report, investigation, "
                "analysis, design doc). The artifact is shown in the web UI's Artifacts "
                "panel. Use this when the user asks for a report, investigation, document, "
                "summary, or any long-form content they'll want to reference later. "
                "Previous versions are preserved automatically for diff comparison. "
                "Do NOT use for code files (those go through git)."
            ),
            handler=_publish_artifact_handler,
            parameters={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Human-readable title for the artifact",
                    },
                    "filename": {
                        "type": "string",
                        "description": "Simple filename (e.g. 'investigation.md'). No paths. Prefer .md extension.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full content of the artifact (markdown recommended)",
                    },
                    "description": {
                        "type": "string",
                        "description": "Brief one-line description of the artifact",
                    },
                },
                "required": ["title", "filename", "content"],
            },
            skip_permission=True,
        )
        custom_tools.append(publish_artifact_tool)

        # Custom tool: consult_panel — get second opinions from expert sub-agents
        # Each panelist plays a distinct archetype to surface orthogonal concerns.
        async def _consult_panel_handler(invocation: object) -> ToolResult:
            args = getattr(invocation, "arguments", {}) or {}
            problem = args.get("problem_description", "").strip()
            if not problem:
                return ToolResult(
                    text_result_for_llm=(
                        "Error: please provide a detailed 'problem_description' "
                        "that includes: what you're trying to solve, what you've "
                        "tried, your current proposed plan (if any), your actual "
                        "constraints (deadline, scope, risk tolerance), AND the "
                        "research you've already done — attach relevant file "
                        "excerpts, command outputs, error messages, and prior "
                        "art. The panel reasons over the evidence you provide; "
                        "don't expect them to do discovery for you."
                    ),
                    result_type="error",
                )

            def _archetype_prompt(role: str, voice: str, focus: str) -> str:
                return (
                    f"You are **{role}**, one member of a 4-person expert panel "
                    "consulted by a fellow engineer who is stuck on a technical "
                    "problem. The other panelists have different perspectives — "
                    "your job is to bring YOUR distinct lens, not to produce a "
                    "balanced take.\n\n"
                    f"**Your voice:** {voice}\n\n"
                    f"**What you look for:** {focus}\n\n"
                    "**DO NOT do your own background research.** The calling "
                    "engineer has already done the investigation and attached "
                    "their findings in the problem description. Reason from "
                    "the evidence they provided. Only fire off tool calls to "
                    "research something if you have a specific idea that isn't "
                    "covered by their attached material AND that idea is "
                    "central to your recommendation — never for general "
                    "background. If you want more evidence, name what's "
                    "missing in your 'sharp question' instead of hunting for "
                    "it yourself.\n\n"
                    "Stay in character. Be direct, specific, and concrete. "
                    "Do NOT hedge with 'it depends' — pick a position and defend "
                    "it. Your perspective will be synthesized with the others, "
                    "so redundancy with a balanced middle-ground is wasted effort.\n\n"
                    "Structure your response as:\n"
                    "1. **Your take** (2-4 sentences: the core point from your lens)\n"
                    "2. **What the engineer is likely missing** (concrete risks or "
                    "opportunities through your lens)\n"
                    "3. **Concrete recommendation** (what you would do, and why)\n"
                    "4. **A sharp question** (one question that if answered would "
                    "materially change the approach)\n\n"
                    "--- Problem Description ---\n"
                    f"{problem}"
                )

            architect_prompt = _archetype_prompt(
                role="The Architect",
                voice=(
                    "'What does this look like in 2 years at 10x scale? Who "
                    "maintains this?' You care about long-term stewardship, "
                    "cohesion, extension points, and keeping the mental model "
                    "clean."
                ),
                focus=(
                    "Coupling, hidden assumptions baked into code, decisions "
                    "that are cheap now but expensive to reverse later, "
                    "abstractions that will or won't hold up, interfaces that "
                    "shape future work. Call out when a quick fix is actually "
                    "a load-bearing decision in disguise."
                ),
            )

            pragmatist_prompt = _archetype_prompt(
                role="The Pragmatist",
                voice=(
                    "'What's the simplest thing that could work? Ship it, "
                    "iterate later.' You distrust complexity, premature "
                    "abstraction, and analysis paralysis. Think VERY hard and "
                    "carefully before speaking — the engineer will rely on "
                    "your judgment about what's truly necessary vs. what's "
                    "gold-plating."
                ),
                focus=(
                    "YAGNI violations, over-engineering, scope creep, "
                    "speculative generality. What's the smallest diff that "
                    "actually solves the user's real problem today? What can "
                    "be deleted, deferred, or faked? Call out when the "
                    "engineer is solving a problem they don't actually have."
                ),
            )

            skeptic_prompt = _archetype_prompt(
                role="The Skeptic",
                voice=(
                    "'How does this fail? What's the attacker's move? What "
                    "if the input is null, malicious, or huge?' You assume "
                    "the happy path is a lie and every assumption is wrong "
                    "until proven otherwise."
                ),
                focus=(
                    "Edge cases, security holes, race conditions, silent "
                    "failures, unvalidated inputs, data integrity, error "
                    "paths, partial failures, concurrency bugs, trust "
                    "boundaries. What inputs break this? What happens on a "
                    "crash mid-operation? What does an adversary do?"
                ),
            )

            contrarian_prompt = _archetype_prompt(
                role="The Contrarian",
                voice=(
                    "'What if the framing is wrong? What if we should do the "
                    "literal opposite?' You question premises, flip "
                    "assumptions, and look for the problem behind the "
                    "problem."
                ),
                focus=(
                    "Unquestioned assumptions in how the problem is framed, "
                    "false dichotomies, wrong-level solutions, cases where "
                    "NOT doing the thing is the right answer. If everyone "
                    "else is agreeing, dig for what they're all missing. "
                    "Surface the option nobody proposed."
                ),
            )

            architect_model = _resolve_model(_PANEL_MODEL_PREFERENCES["architect"])
            pragmatist_model = _resolve_model(_PANEL_MODEL_PREFERENCES["pragmatist"])
            skeptic_model = _resolve_model(_PANEL_MODEL_PREFERENCES["skeptic"])
            contrarian_model = _resolve_model(_PANEL_MODEL_PREFERENCES["contrarian"])

            # Launch all 4 panelists in parallel via copilot -p subprocesses.
            try:
                import copilot as _copilot_pkg
                cli_dir = os.path.dirname(_copilot_pkg.__file__)
                cli_bin = os.path.join(cli_dir, "bin", "copilot")
            except Exception:
                cli_bin = "copilot"

            panelists = [
                ("The Architect", architect_model, architect_prompt),
                ("The Pragmatist", pragmatist_model, pragmatist_prompt),
                ("The Skeptic", skeptic_model, skeptic_prompt),
                ("The Contrarian", contrarian_model, contrarian_prompt),
            ]

            async def _run_panelist(
                name: str, model: str, prompt: str,
            ) -> tuple[str, str]:
                """Run a single panelist and return (name, response)."""
                try:
                    proc = await asyncio.create_subprocess_exec(
                        cli_bin, "-p", prompt,
                        "--model", model,
                        "--no-auto-update",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env={**os.environ, "NO_COLOR": "1"},
                    )
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(), timeout=180.0,
                    )
                    text = stdout.decode("utf-8", errors="replace").strip()
                    if not text:
                        err = stderr.decode("utf-8", errors="replace").strip()
                        return (name, f"[No response. stderr: {err[:300]}]")
                    # Strip CLI decoration lines
                    lines = text.split("\n")
                    cleaned = [
                        ln for ln in lines
                        if not ln.startswith("●") and not ln.strip().startswith("└ {")
                    ]
                    return (name, "\n".join(cleaned).strip() or text)
                except asyncio.TimeoutError:
                    return (name, "[Timed out after 180s]")
                except Exception as e:
                    return (name, f"[Error: {e}]")

            print(
                f"[agent] consult_panel: launching 4 panelists "
                f"({architect_model}, {pragmatist_model}, "
                f"{skeptic_model}, {contrarian_model})",
                file=sys.stderr,
            )

            results = await asyncio.gather(*(
                _run_panelist(name, model, prompt)
                for name, model, prompt in panelists
            ))

            # Format consolidated response
            sections = []
            for name, response in results:
                sections.append(f"## {name}\n\n{response}")

            consolidated = "\n\n---\n\n".join(sections)
            return ToolResult(
                text_result_for_llm=(
                    "Here are the four panel perspectives. Expect sharp "
                    "disagreement — that's where the signal is. Synthesize "
                    "the takes that best fit your actual constraints; don't "
                    "try to please all four.\n\n"
                    + consolidated
                ),
            )

        consult_panel_tool = Tool(
            name="consult_panel",
            description=(
                "Convene a 4-person panel of expert agents with deliberately "
                "different archetypes (Architect, Pragmatist, Skeptic, "
                "Contrarian) at high-leverage decision points. Use BEFORE "
                "starting any large new piece of work, when designing an API "
                "or architecture, when choosing between viable approaches, on "
                "the 2nd attempt at a problem, or when stuck. Each panelist "
                "brings a distinct lens rather than a balanced take — expect "
                "sharp disagreement, that's where the signal is. YOU must do "
                "the research first and attach findings (file excerpts, "
                "command output, errors, prior art) and your proposed plan; "
                "the panel reasons over your evidence rather than doing "
                "their own discovery."
            ),
            handler=_consult_panel_handler,
            skip_permission=True,
            parameters={
                "type": "object",
                "properties": {
                    "problem_description": {
                        "type": "string",
                        "description": (
                            "A detailed brief for the panel. Must include: "
                            "(a) what you're trying to solve and why; "
                            "(b) what you've tried and the result; "
                            "(c) your current proposed plan or approach; "
                            "(d) real constraints (deadline, scope, risk); "
                            "(e) research you've already done — attach "
                            "relevant file excerpts, command outputs, error "
                            "messages, and any prior art you've considered. "
                            "The panel critiques evidence, they do not "
                            "replace discovery work."
                        ),
                    },
                },
                "required": ["problem_description"],
            },
        )
        custom_tools.append(consult_panel_tool)

        # ── Custom tool: web_search ──
        async def _web_search_handler(invocation: object) -> ToolResult:
            """Run a web search via the Copilot CLI in -p mode."""
            args = getattr(invocation, "arguments", {}) or {}
            query = args.get("query", "").strip()
            if not query:
                return ToolResult(
                    text_result_for_llm="Error: 'query' parameter is required",
                    result_type="error",
                )
            # Find the copilot CLI binary (bundled with the SDK)
            try:
                import copilot as _copilot_pkg
                cli_dir = os.path.dirname(_copilot_pkg.__file__)
                cli_bin = os.path.join(cli_dir, "bin", "copilot")
            except Exception:
                cli_bin = "copilot"

            prompt = (
                f"Use the web_search tool to search for: {query}\n"
                "Return ONLY the search result text. No commentary, no preamble."
            )
            try:
                proc = await asyncio.create_subprocess_exec(
                    cli_bin, "-p", prompt,
                    "--model", "claude-sonnet-4.6",
                    "--allow-all-tools", "--no-auto-update",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env={**os.environ, "NO_COLOR": "1"},
                )
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=60.0,
                )
                result_text = stdout.decode("utf-8", errors="replace").strip()
                if not result_text:
                    err = stderr.decode("utf-8", errors="replace").strip()
                    return ToolResult(
                        text_result_for_llm=f"Web search returned no results. stderr: {err[:500]}",
                        result_type="error",
                    )
                # Strip the tool-call decoration line the CLI prepends
                lines = result_text.split("\n")
                cleaned = []
                skip_next_blank = False
                for line in lines:
                    if line.startswith("● Web Search") or line.strip().startswith("└ {"):
                        skip_next_blank = True
                        continue
                    if skip_next_blank and not line.strip():
                        skip_next_blank = False
                        continue
                    skip_next_blank = False
                    cleaned.append(line)
                return ToolResult(
                    text_result_for_llm="\n".join(cleaned).strip() or result_text,
                )
            except asyncio.TimeoutError:
                return ToolResult(
                    text_result_for_llm="Web search timed out after 60s",
                    result_type="error",
                )
            except Exception as e:
                return ToolResult(
                    text_result_for_llm=f"Web search failed: {e}",
                    result_type="error",
                )

        web_search_tool = Tool(
            name="web_search",
            description=(
                "Search the web for current information. Returns an AI-generated "
                "summary with citations. Use when you need up-to-date information, "
                "recent events, documentation, or facts you're unsure about."
            ),
            handler=_web_search_handler,
            skip_permission=True,
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "A clear, specific search query. Example: "
                            "'Python asyncio best practices 2025'"
                        ),
                    },
                },
                "required": ["query"],
            },
        )
        custom_tools.append(web_search_tool)

        # ── Load plugin tools ──
        try:
            from enclave.agent.plugins import discover_plugins
            plugin_tools = discover_plugins(workspace=working_directory)
            for pt in plugin_tools:
                tool = Tool(
                    name=pt.name,
                    description=pt.description,
                    handler=pt.handler,
                    parameters=pt.parameters,
                )
                custom_tools.append(tool)
                print(f"[agent] Plugin tool loaded: {pt.name} (from {pt.source_file})",
                      file=sys.stderr)
        except Exception as e:
            print(f"[agent] Plugin discovery failed: {e}", file=sys.stderr)

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
                    streaming=True,
                )
                print(f"[agent] Session resumed: {last_id}", file=sys.stderr)
                await _configure_model(session, client)
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
            streaming=True,
        )
        await _configure_model(session, client)
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
    state = AgentState()
    state.ipc = ipc
    state.loop = loop
    state.working_directory = os.environ.get("ENCLAVE_WORKSPACE", os.getcwd())

    sdk_result = await try_init_copilot(
        working_directory=state.working_directory, ipc=ipc,
        agent_state=state,
    )
    if sdk_result:
        state.sdk_client, state.sdk_session = sdk_result
        print("[agent] Copilot SDK initialized", file=sys.stderr)
        # Register persistent event listener (handles background agents too)
        try:
            state.listener_ctl = setup_session_listener(ipc, state.sdk_session, loop, state)
            print("[agent] Persistent event listener registered", file=sys.stderr)
        except ImportError:
            print("[agent] SessionEventType not available, running in echo mode", file=sys.stderr)
            state.sdk_client, state.sdk_session = None, None
    else:
        print("[agent] Running in echo mode (no Copilot SDK)", file=sys.stderr)

    # Send ready status
    await ipc.send(Message(
        type=MessageType.STATUS_UPDATE,
        payload={
            "status": "ready",
            "session_id": session_id,
            "copilot_available": state.sdk_session is not None,
        },
    ))

    # Report nix-shell status if one was requested
    nix_shell_requested = os.environ.get("ENCLAVE_NIX_SHELL", "")
    nix_shell_failed = os.environ.get("ENCLAVE_NIX_SHELL_FAILED", "")
    nix_shell_log = os.environ.get("ENCLAVE_NIX_SHELL_LOG", "")
    if nix_shell_requested:
        if nix_shell_failed:
            print(f"[agent] nix-shell FAILED for {nix_shell_requested}", file=sys.stderr)
            # Read the log so the SDK has context about what went wrong
            log_content = ""
            if nix_shell_log:
                try:
                    log_content = Path(nix_shell_log).read_text()[-2000:]
                except Exception:
                    log_content = "(could not read log)"
            await ipc.send(Message(
                type=MessageType.STATUS_UPDATE,
                payload={
                    "status": "nix_shell_failed",
                    "path": nix_shell_requested,
                    "log": nix_shell_log,
                    "log_content": log_content,
                },
            ))
        else:
            print(f"[agent] Running under nix-shell: {nix_shell_requested}", file=sys.stderr)
            await ipc.send(Message(
                type=MessageType.STATUS_UPDATE,
                payload={
                    "status": "nix_shell_active",
                    "path": nix_shell_requested,
                },
            ))

    # Register message handlers
    async def on_user_message(msg: Message) -> Message | None:
        # Real user input resets doom-loop tracking — the heuristic exists
        # to catch agents stuck without feedback. A reply from the user
        # IS the feedback, so any past stuck-pattern accrual is no longer
        # actionable. (Synthetic scheduler callbacks below intentionally
        # don't reset.)
        if state is not None:
            state.task_start_time = 0.0
            state.consecutive_turns = 0
            state.consecutive_failures = 0
            state.doom_loop_nudged_at = 0
            state.doom_loop_nudge_count = 0
            state.recent_edit_targets.clear()
            state.recent_bash_commands.clear()
        await handle_user_message(state, msg)
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
        await handle_user_message(state, synth)
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
            if not state.sdk_session:
                print("[agent] No SDK session for dreaming", file=sys.stderr)
                return None
            # Use the SDK to extract memories from the current context
            turn = state.sdk_session.send(
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
        if not state.sdk_session:
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
            await state.sdk_session.send(notification)
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
    if state.listener_ctl and callable(state.listener_ctl):
        try:
            state.listener_ctl()
        except Exception:
            pass
    if state.sdk_session:
        try:
            await state.sdk_session.disconnect()
        except Exception:
            pass
    if state.sdk_client:
        try:
            await state.sdk_client.stop()
        except Exception:
            pass

    await ipc.disconnect()
    print("[agent] Shut down", file=sys.stderr)


if __name__ == "__main__":
    # Ensure stdout/stderr are unbuffered so logs appear in podman logs
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)

    # Landlock sandbox is disabled for now — container isolation is the
    # primary security boundary.  Host-mode sessions rely on user-level
    # permissions until Landlock policy is refined.
    # if os.environ.get("ENCLAVE_HOST_MODE") == "1":
    #     ...

    asyncio.run(main())
