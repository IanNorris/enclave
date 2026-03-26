"""MCP (Model Context Protocol) server for Enclave.

Exposes Enclave sessions, audit logs, cost tracking, and management
capabilities as MCP tools and resources. External tools (VS Code, other
agents) can interact with Enclave through this server.

Usage:
    # Standalone
    python -m enclave.orchestrator.mcp_server

    # Or via enclavectl
    enclavectl mcp
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from enclave.common.config import load_config

# ── Initialise MCP server ──

mcp = FastMCP(
    "Enclave",
    instructions=(
        "Enclave AI Agent Orchestrator — manage sandboxed AI agent "
        "sessions, view audit logs, check costs, and monitor system health."
    ),
)


def _get_config():
    """Load Enclave configuration."""
    config_path = os.environ.get(
        "ENCLAVE_CONFIG",
        str(Path.home() / ".config" / "enclave" / "enclave.yaml"),
    )
    if not Path(config_path).exists():
        return None
    return load_config(config_path)


def _get_sessions() -> list[dict]:
    """Load sessions from the session store."""
    config = _get_config()
    if not config:
        return []
    sessions_file = Path(config.container.session_base) / "sessions.json"
    if not sessions_file.exists():
        return []
    try:
        data = json.loads(sessions_file.read_text())
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _get_data_dir() -> str:
    """Get the Enclave data directory."""
    config = _get_config()
    if config:
        return config.container.session_base.replace("/sessions", "")
    return str(Path.home() / ".local" / "share" / "enclave")


# ── Resources ──


@mcp.resource("enclave://sessions")
def list_sessions() -> str:
    """List all Enclave agent sessions with their status."""
    sessions = _get_sessions()
    if not sessions:
        return "No sessions found."

    lines = []
    for s in sessions:
        status = s.get("status", "unknown")
        emoji = "🟢" if status == "running" else "⚫"
        lines.append(
            f"{emoji} **{s.get('name', '?')}** ({s.get('id', '?')[:12]}) "
            f"— {status}, profile={s.get('profile', '?')}"
        )
    return "\n".join(lines)


@mcp.resource("enclave://sessions/{session_id}")
def get_session(session_id: str) -> str:
    """Get details for a specific session."""
    sessions = _get_sessions()
    for s in sessions:
        if s.get("id", "").startswith(session_id):
            return json.dumps(s, indent=2)
    return f"Session not found: {session_id}"


@mcp.resource("enclave://audit")
def get_audit_log() -> str:
    """Get recent global audit log entries."""
    from enclave.common.audit import AuditLog
    audit = AuditLog(_get_data_dir())
    entries = audit.read_global(tail=50)
    if not entries:
        return "No audit entries."
    return "\n".join(json.dumps(e) for e in entries)


@mcp.resource("enclave://costs")
def get_cost_summary() -> str:
    """Get global token usage and cost estimates."""
    from enclave.common.cost_tracker import CostTracker
    tracker = CostTracker(_get_data_dir())
    stats = tracker.global_stats()
    tracker.close()
    return json.dumps(stats, indent=2)


# ── Tools ──


@mcp.tool()
def sessions_list() -> str:
    """List all Enclave agent sessions."""
    sessions = _get_sessions()
    result = []
    for s in sessions:
        result.append({
            "id": s.get("id", ""),
            "name": s.get("name", ""),
            "status": s.get("status", ""),
            "profile": s.get("profile", ""),
            "created_at": s.get("created_at", ""),
        })
    return json.dumps(result, indent=2)


@mcp.tool()
def session_info(session_id: str) -> str:
    """Get detailed information about a specific session.

    Args:
        session_id: Full or partial session ID.
    """
    sessions = _get_sessions()
    for s in sessions:
        if s.get("id", "").startswith(session_id):
            return json.dumps(s, indent=2)
    return json.dumps({"error": f"Session not found: {session_id}"})


@mcp.tool()
def audit_log(session_id: str = "", tail: int = 30) -> str:
    """View recent audit log entries.

    Args:
        session_id: Optional session ID to filter by (empty for global).
        tail: Number of recent entries to return.
    """
    from enclave.common.audit import AuditLog
    audit = AuditLog(_get_data_dir())

    if session_id:
        entries = audit.read_session(session_id, tail=tail)
    else:
        entries = audit.read_global(tail=tail)

    return json.dumps(entries, indent=2)


@mcp.tool()
def cost_stats(session_id: str = "") -> str:
    """Get token usage and cost statistics.

    Args:
        session_id: Optional session ID (empty for global stats).
    """
    from enclave.common.cost_tracker import CostTracker
    tracker = CostTracker(_get_data_dir())

    if session_id:
        stats = tracker.session_stats(session_id)
    else:
        stats = tracker.global_stats()

    tracker.close()
    return json.dumps(stats, indent=2)


@mcp.tool()
def session_audit(session_id: str) -> str:
    """Get audit trail for a specific session.

    Args:
        session_id: Session ID to get audit for.
    """
    from enclave.common.audit import AuditLog
    audit = AuditLog(_get_data_dir())
    entries = audit.read_session(session_id)
    return json.dumps(entries, indent=2)


@mcp.tool()
def system_status() -> str:
    """Get Enclave system status overview."""
    import subprocess

    sessions = _get_sessions()
    active = [s for s in sessions if s.get("status") == "running"]
    stopped = [s for s in sessions if s.get("status") == "stopped"]

    # Check systemd service
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "enclave"],
            capture_output=True, text=True, timeout=5,
        )
        svc_status = result.stdout.strip()
    except Exception:
        svc_status = "unknown"

    return json.dumps({
        "service_status": svc_status,
        "sessions_running": len(active),
        "sessions_stopped": len(stopped),
        "sessions_total": len(sessions),
    }, indent=2)


# ── Entry point ──

def run_mcp_server():
    """Run the MCP server (stdio transport by default)."""
    mcp.run()


if __name__ == "__main__":
    run_mcp_server()
