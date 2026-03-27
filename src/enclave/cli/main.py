"""Enclave CLI — command-line management interface.

Non-interactive commands for quick management tasks:
    enclavectl status       — System overview
    enclavectl sessions     — List sessions with resource usage
    enclavectl logs <id>    — Tail logs for a session
    enclavectl stop <id>    — Stop a session
    enclavectl cleanup      — Clean up stopped sessions
    enclavectl top          — Live resource overview (refreshing)
    enclavectl tui          — Launch interactive TUI
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import box

from enclave.common.config import load_config


console = Console()


def _load_config():
    """Load config from default or environment path."""
    config_path = os.environ.get(
        "ENCLAVE_CONFIG",
        str(Path.home() / ".config" / "enclave" / "enclave.yaml"),
    )
    if not Path(config_path).exists():
        console.print(f"[red]Config not found:[/red] {config_path}")
        sys.exit(1)
    return load_config(config_path)


def _get_sessions(config) -> list[dict]:
    """Get sessions from the session store."""
    sessions_file = Path(config.container.session_base) / "sessions.json"
    if not sessions_file.exists():
        return []
    try:
        data = json.loads(sessions_file.read_text())
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _get_container_stats(container_id: str) -> dict:
    """Get resource stats for a container via podman."""
    try:
        result = subprocess.run(
            ["podman", "stats", "--no-stream", "--format", "json", container_id],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            stats = json.loads(result.stdout)
            if stats:
                return stats[0] if isinstance(stats, list) else stats
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        pass
    return {}


def _get_container_processes(container_id: str) -> list[str]:
    """Get process list for a container via podman top."""
    try:
        result = subprocess.run(
            ["podman", "top", container_id],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip().split("\n")
    except (subprocess.TimeoutExpired, OSError):
        pass
    return []


def _workspace_size(path: str) -> str:
    """Get workspace disk usage."""
    try:
        result = subprocess.run(
            ["du", "-sh", path],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.split()[0]
    except (subprocess.TimeoutExpired, OSError):
        pass
    return "?"


def _systemd_status() -> tuple[str, str]:
    """Get systemd service status."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "enclave"],
            capture_output=True, text=True, timeout=5,
        )
        active = result.stdout.strip()
        result2 = subprocess.run(
            ["systemctl", "--user", "show", "enclave",
             "--property=ActiveEnterTimestamp", "--value"],
            capture_output=True, text=True, timeout=5,
        )
        since = result2.stdout.strip() if result2.returncode == 0 else ""
        return active, since
    except (subprocess.TimeoutExpired, OSError):
        return "unknown", ""


# ------------------------------------------------------------------
# Commands
# ------------------------------------------------------------------


def cmd_status(args):
    """Show system status overview."""
    config = _load_config()
    sessions = _get_sessions(config)
    active = [s for s in sessions if s.get("status") == "running"]
    stopped = [s for s in sessions if s.get("status") == "stopped"]

    svc_status, svc_since = _systemd_status()
    status_color = "green" if svc_status == "active" else "red"

    # Header
    console.print()
    console.print(Panel(
        Text.from_markup(
            f"[bold]🏰 Enclave[/bold]\n\n"
            f"  Service:  [{status_color}]{svc_status}[/{status_color}]"
            f"{f'  (since {svc_since})' if svc_since else ''}\n"
            f"  Sessions: [cyan]{len(active)}[/cyan] running, "
            f"[dim]{len(stopped)}[/dim] stopped, "
            f"{len(sessions)} total\n"
            f"  Config:   {os.environ.get('ENCLAVE_CONFIG', '~/.config/enclave/enclave.yaml')}"
        ),
        title="System Status",
        border_style="blue",
    ))

    if active:
        table = Table(
            title="Active Sessions",
            box=box.ROUNDED,
            show_lines=True,
        )
        table.add_column("Session ID", style="cyan", no_wrap=True)
        table.add_column("Name", style="bold")
        table.add_column("Profile", style="dim")
        table.add_column("Workspace", justify="right")
        table.add_column("Created", style="dim")

        for s in active:
            ws_size = _workspace_size(s.get("workspace_path", ""))
            created = s.get("created_at", "")[:16]
            table.add_row(
                s.get("id", "?"),
                s.get("name", "?"),
                s.get("profile", "?"),
                ws_size,
                created,
            )

        console.print(table)
    console.print()


def cmd_sessions(args):
    """List all sessions with details."""
    config = _load_config()
    sessions = _get_sessions(config)

    if not sessions:
        console.print("[dim]No sessions found.[/dim]")
        return

    table = Table(box=box.ROUNDED, show_lines=True)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Name", style="bold")
    table.add_column("Status")
    table.add_column("Profile", style="dim")
    table.add_column("Image", style="dim", max_width=30)
    table.add_column("Workspace", justify="right")
    table.add_column("Created", style="dim")

    for s in sessions:
        status = s.get("status", "?")
        if status == "running":
            status_styled = "[green]● running[/green]"
        elif status == "stopped":
            status_styled = "[red]○ stopped[/red]"
        else:
            status_styled = f"[yellow]{status}[/yellow]"

        ws_size = _workspace_size(s.get("workspace_path", "")) if args.size else ""
        created = s.get("created_at", "")[:16]

        table.add_row(
            s.get("id", "?"),
            s.get("name", "?"),
            status_styled,
            s.get("profile", "?"),
            s.get("image", "?"),
            ws_size,
            created,
        )

    console.print(table)


def cmd_top(args):
    """Live resource monitor for running containers."""
    config = _load_config()
    sessions = _get_sessions(config)
    active = [s for s in sessions if s.get("status") == "running"]

    if not active:
        console.print("[dim]No running sessions.[/dim]")
        return

    container_ids = [s.get("container_id", "") for s in active if s.get("container_id")]
    if not container_ids:
        console.print("[dim]No containers with IDs found.[/dim]")
        return

    # Use podman stats directly (it has a nice live view)
    try:
        os.execvp("podman", ["podman", "stats"] + container_ids)
    except OSError as e:
        console.print(f"[red]Failed to run podman stats: {e}[/red]")


def cmd_logs(args):
    """Tail logs for a session."""
    config = _load_config()
    sessions = _get_sessions(config)

    session = None
    for s in sessions:
        if s.get("id") == args.session_id:
            session = s
            break

    if session is None:
        console.print(f"[red]Session not found:[/red] {args.session_id}")
        sys.exit(1)

    container_id = session.get("container_id", "")
    if not container_id:
        console.print("[red]No container ID for this session.[/red]")
        sys.exit(1)

    follow = ["--follow"] if args.follow else []
    tail = ["--tail", str(args.tail)]

    try:
        os.execvp("podman", ["podman", "logs"] + follow + tail + [container_id])
    except OSError as e:
        console.print(f"[red]Failed to run podman logs: {e}[/red]")


def cmd_stop(args):
    """Stop a running session."""
    config = _load_config()
    sessions = _get_sessions(config)

    session = None
    for s in sessions:
        if s.get("id") == args.session_id:
            session = s
            break

    if session is None:
        console.print(f"[red]Session not found:[/red] {args.session_id}")
        sys.exit(1)

    if session.get("status") != "running":
        console.print(f"[yellow]Session is already {session.get('status')}.[/yellow]")
        return

    container_id = session.get("container_id", "")
    host_pid = session.get("host_pid")
    sid = args.session_id

    stopped = False

    # Try stopping by container ID first
    if container_id:
        result = subprocess.run(
            ["podman", "stop", container_id],
            capture_output=True, text=True, timeout=30,
        )
        stopped = result.returncode == 0

    # Try stopping by container name (podman names match session ID)
    if not stopped:
        result = subprocess.run(
            ["podman", "stop", sid],
            capture_output=True, text=True, timeout=30,
        )
        stopped = result.returncode == 0

    # Try host PID
    if not stopped and host_pid:
        import signal
        try:
            os.kill(host_pid, signal.SIGTERM)
            stopped = True
        except (ProcessLookupError, PermissionError):
            pass

    if stopped:
        _mark_session_stopped(config, sid)
        console.print(f"[green]✅ Stopped session[/green] {sid}")
    else:
        # Even if we can't kill it, mark it stopped to prevent auto-restore
        _mark_session_stopped(config, sid)
        console.print(f"[yellow]Marked session {sid} as stopped (process may already be gone).[/yellow]")


def _mark_session_stopped(config, session_id: str):
    """Update session status to stopped in sessions.json."""
    sessions_file = Path(config.container.session_base) / "sessions.json"
    if not sessions_file.exists():
        return
    try:
        data = json.loads(sessions_file.read_text())
        if not isinstance(data, list):
            return
        for s in data:
            if s.get("id") == session_id:
                s["status"] = "stopped"
                s["host_pid"] = None
                break
        sessions_file.write_text(json.dumps(data, indent=2))
    except (json.JSONDecodeError, OSError):
        pass


def cmd_cleanup(args):
    """List or clean up stopped sessions."""
    config = _load_config()
    sessions = _get_sessions(config)
    stopped = [s for s in sessions if s.get("status") == "stopped"]

    if not stopped:
        console.print("[dim]No stopped sessions to clean up.[/dim]")
        return

    if not args.confirm:
        table = Table(title="Stopped Sessions", box=box.ROUNDED)
        table.add_column("ID", style="cyan")
        table.add_column("Name")
        table.add_column("Workspace")
        table.add_column("Created", style="dim")

        for s in stopped:
            ws = _workspace_size(s.get("workspace_path", ""))
            table.add_row(
                s.get("id", "?"),
                s.get("name", "?"),
                ws,
                s.get("created_at", "")[:16],
            )

        console.print(table)
        console.print(
            f"\n[yellow]Use --yes to clean up all {len(stopped)} stopped sessions,[/yellow]\n"
            f"[yellow]or use the Matrix control room: cleanup all[/yellow]"
        )
        return

    console.print(f"Cleaning up {len(stopped)} stopped sessions...")
    for s in stopped:
        cid = s.get("container_id", "")
        if cid:
            subprocess.run(
                ["podman", "rm", "-f", cid],
                capture_output=True, timeout=10,
            )
        console.print(f"  [green]✓[/green] {s.get('id')}")
    console.print(f"\n[green]Cleaned up {len(stopped)} sessions.[/green]")


def cmd_tui(args):
    """Launch the interactive TUI."""
    from enclave.cli.tui import run_tui
    run_tui()


def cmd_audit(args):
    """Show recent audit log entries."""
    config = _load_config()
    data_dir = config.container.session_base.replace("/sessions", "")
    from enclave.common.audit import AuditLog
    audit = AuditLog(data_dir)

    if args.session_id:
        entries = audit.read_session(args.session_id, tail=args.tail)
        title = f"Audit: {args.session_id}"
    else:
        entries = audit.read_global(tail=args.tail)
        title = "Audit: global"

    if not entries:
        console.print("[dim]No audit entries found.[/dim]")
        return

    table = Table(title=title, box=box.SIMPLE_HEAVY)
    table.add_column("Time", style="dim", width=19)
    table.add_column("Event", style="bold")
    table.add_column("Session", width=12)
    table.add_column("Details")

    for entry in entries:
        ts = entry.get("ts", "")[:19].replace("T", " ")
        event = entry.get("event", "")
        sid = entry.get("session_id", "")[:12]
        # Build details from remaining keys
        skip = {"ts", "event", "session_id", "user"}
        details_parts = []
        if entry.get("user"):
            details_parts.append(f"user={entry['user']}")
        for k, v in entry.items():
            if k not in skip:
                details_parts.append(f"{k}={v}")
        details = " ".join(details_parts)
        if len(details) > 80:
            details = details[:77] + "..."
        table.add_row(ts, event, sid, details)

    console.print(table)


def cmd_costs(args):
    """Show token usage and cost estimates."""
    config = _load_config()
    data_dir = config.container.session_base.replace("/sessions", "")
    from enclave.common.cost_tracker import CostTracker
    tracker = CostTracker(data_dir)

    if args.session_id:
        stats = tracker.session_stats(args.session_id)
        console.print(Panel(
            Text.from_markup(
                f"  Session:      [cyan]{args.session_id}[/cyan]\n"
                f"  Input tokens: {stats['total_input_tokens']:,}\n"
                f"  Output tokens:{stats['total_output_tokens']:,}\n"
                f"  Total tokens: {stats['total_tokens']:,}\n"
                f"  Turns:        {stats['turn_count']}\n"
                f"  Est. cost:    [green]${stats['estimated_cost_usd']:.4f}[/green]"
            ),
            title="Session Usage",
            border_style="blue",
        ))

        budget = tracker.check_budget(args.session_id)
        if budget:
            pct = budget["percent_used"]
            color = "red" if budget["over_budget"] else ("yellow" if budget["alert"] else "green")
            console.print(f"\n  Budget: [{color}]{pct:.1f}%[/{color}] used "
                          f"({budget['used_tokens']:,} / {budget['max_tokens']:,})")
    else:
        stats = tracker.global_stats()
        console.print(Panel(
            Text.from_markup(
                f"  Sessions:     {stats['session_count']}\n"
                f"  Input tokens: {stats['total_input_tokens']:,}\n"
                f"  Output tokens:{stats['total_output_tokens']:,}\n"
                f"  Total tokens: {stats['total_tokens']:,}\n"
                f"  Total turns:  {stats['turn_count']}\n"
                f"  Est. cost:    [green]${stats['estimated_cost_usd']:.4f}[/green]"
            ),
            title="Global Token Usage",
            border_style="blue",
        ))

        # Per-session breakdown
        sessions = _get_sessions(config)
        if sessions:
            table = Table(title="Per-Session Breakdown", box=box.SIMPLE_HEAVY)
            table.add_column("Session", style="cyan", width=12)
            table.add_column("Name", style="bold")
            table.add_column("Tokens", justify="right")
            table.add_column("Turns", justify="right")
            table.add_column("Cost", justify="right", style="green")

            for s in sessions:
                sid = s.get("id", "")
                ss = tracker.session_stats(sid)
                if ss["total_tokens"] > 0:
                    table.add_row(
                        sid[:12],
                        s.get("name", ""),
                        f"{ss['total_tokens']:,}",
                        str(ss["turn_count"]),
                        f"${ss['estimated_cost_usd']:.4f}",
                    )
            console.print(table)

    tracker.close()


def cmd_mcp(args):
    """Start the MCP server."""
    from enclave.orchestrator.mcp_server import run_mcp_server
    run_mcp_server()


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="enclavectl",
        description="🏰 Enclave — AI Agent Orchestrator Management",
    )
    sub = parser.add_subparsers(dest="command")

    # status
    sub.add_parser("status", help="System status overview")

    # sessions
    sessions_p = sub.add_parser("sessions", help="List all sessions")
    sessions_p.add_argument("--size", action="store_true", help="Show workspace sizes")

    # top
    sub.add_parser("top", help="Live resource monitor")

    # logs
    logs_p = sub.add_parser("logs", help="Tail session logs")
    logs_p.add_argument("session_id", help="Session ID")
    logs_p.add_argument("-f", "--follow", action="store_true", help="Follow log output")
    logs_p.add_argument("-n", "--tail", type=int, default=50, help="Number of lines")

    # stop
    stop_p = sub.add_parser("stop", help="Stop a session")
    stop_p.add_argument("session_id", help="Session ID")

    # cleanup
    cleanup_p = sub.add_parser("cleanup", help="Clean up stopped sessions")
    cleanup_p.add_argument("--yes", dest="confirm", action="store_true", help="Actually clean up")

    # tui
    sub.add_parser("tui", help="Interactive terminal dashboard")

    # audit
    audit_p = sub.add_parser("audit", help="Show recent audit log entries")
    audit_p.add_argument("session_id", nargs="?", default="", help="Session ID (omit for global)")
    audit_p.add_argument("-n", "--tail", type=int, default=50, help="Number of entries")

    # costs
    costs_p = sub.add_parser("costs", help="Show token usage and cost estimates")
    costs_p.add_argument("session_id", nargs="?", default="", help="Session ID (omit for global)")

    # mcp
    sub.add_parser("mcp", help="Start MCP server (stdio transport)")

    args = parser.parse_args()

    commands = {
        "status": cmd_status,
        "sessions": cmd_sessions,
        "top": cmd_top,
        "logs": cmd_logs,
        "stop": cmd_stop,
        "cleanup": cmd_cleanup,
        "tui": cmd_tui,
        "audit": cmd_audit,
        "costs": cmd_costs,
        "mcp": cmd_mcp,
    }

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    handler = commands.get(args.command)
    if handler:
        handler(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
