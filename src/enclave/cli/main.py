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
    if container_id:
        result = subprocess.run(
            ["podman", "stop", container_id],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            console.print(f"[green]✅ Stopped session[/green] {args.session_id}")
        else:
            console.print(f"[red]Failed to stop: {result.stderr.strip()}[/red]")
    else:
        console.print("[red]No container ID for this session.[/red]")


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

    args = parser.parse_args()

    commands = {
        "status": cmd_status,
        "sessions": cmd_sessions,
        "top": cmd_top,
        "logs": cmd_logs,
        "stop": cmd_stop,
        "cleanup": cmd_cleanup,
        "tui": cmd_tui,
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
