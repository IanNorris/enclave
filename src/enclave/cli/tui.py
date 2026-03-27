"""Enclave interactive TUI — live terminal dashboard.

Real-time monitoring of Enclave sessions, resource usage, and logs.
Built with Textual for a rich terminal experience.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.timer import Timer
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Label,
    Log,
    Static,
    TabbedContent,
    TabPane,
)

from enclave.common.config import load_config


def _load_config():
    """Load config from default or environment path."""
    config_path = os.environ.get(
        "ENCLAVE_CONFIG",
        str(Path.home() / ".config" / "enclave" / "enclave.yaml"),
    )
    if Path(config_path).exists():
        return load_config(config_path)
    return None


def _get_sessions(config) -> list[dict]:
    """Get sessions from the session store."""
    if config is None:
        return []
    sessions_file = Path(config.container.session_base) / "sessions.json"
    if not sessions_file.exists():
        return []
    try:
        data = json.loads(sessions_file.read_text())
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _systemd_status() -> tuple[str, str, str]:
    """Get systemd service status, uptime, and memory."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "enclave"],
            capture_output=True, text=True, timeout=5,
        )
        active = result.stdout.strip()

        result2 = subprocess.run(
            ["systemctl", "--user", "show", "enclave",
             "--property=ActiveEnterTimestamp,MemoryCurrent", "--value"],
            capture_output=True, text=True, timeout=5,
        )
        lines = result2.stdout.strip().split("\n") if result2.returncode == 0 else ["", ""]
        since = lines[0] if len(lines) > 0 else ""
        mem_bytes = lines[1] if len(lines) > 1 else ""

        mem_str = ""
        try:
            mem_mb = int(mem_bytes) / (1024 * 1024)
            mem_str = f"{mem_mb:.0f} MB"
        except (ValueError, TypeError):
            pass

        return active, since, mem_str
    except (subprocess.TimeoutExpired, OSError):
        return "unknown", "", ""


def _workspace_size(path: str) -> str:
    """Get workspace disk usage."""
    if not path or not Path(path).exists():
        return "—"
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


def _container_stats_all() -> dict[str, dict]:
    """Get stats for all running containers."""
    try:
        result = subprocess.run(
            ["podman", "stats", "--no-stream", "--format", "json"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            stats = json.loads(result.stdout)
            if isinstance(stats, list):
                return {s.get("id", "")[:12]: s for s in stats}
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        pass
    return {}


def _mark_session_stopped(config, session_id: str):
    """Update session status to stopped in sessions.json."""
    if config is None:
        return
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


def _get_journal_lines(n: int = 50) -> list[str]:
    """Get recent journal lines for enclave service."""
    try:
        result = subprocess.run(
            ["journalctl", "--user", "-u", "enclave", "--no-pager",
             "-n", str(n), "--output", "short-iso"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip().split("\n")
    except (subprocess.TimeoutExpired, OSError):
        pass
    return ["(no logs available)"]


# ------------------------------------------------------------------
# Widgets
# ------------------------------------------------------------------


class StatusBar(Static):
    """Top status bar showing service health."""

    def compose(self) -> ComposeResult:
        yield Label("Loading...", id="status-text")

    def update_status(self, active: str, since: str, mem: str, session_count: int, running: int):
        indicator = "🟢" if active == "active" else "🔴"
        text = (
            f"{indicator} Enclave: [bold]{active}[/bold]"
            f"  │  Sessions: [cyan]{running}[/cyan] running / {session_count} total"
        )
        if mem:
            text += f"  │  Memory: [yellow]{mem}[/yellow]"
        if since:
            text += f"  │  Since: [dim]{since}[/dim]"

        label = self.query_one("#status-text", Label)
        label.update(text)


class SessionsTable(Static):
    """Table showing all sessions."""

    def compose(self) -> ComposeResult:
        table = DataTable(id="sessions-dt")
        table.cursor_type = "row"
        yield table

    def on_mount(self):
        table = self.query_one("#sessions-dt", DataTable)
        table.add_columns(
            "Status", "Session ID", "Name", "Profile",
            "CPU", "Memory", "Disk", "Created",
        )

    def update_sessions(self, sessions: list[dict], stats: dict[str, dict]):
        table = self.query_one("#sessions-dt", DataTable)
        table.clear()

        for s in sessions:
            status = s.get("status", "?")
            if status == "running":
                status_str = "● running"
            elif status == "stopped":
                status_str = "○ stopped"
            else:
                status_str = f"  {status}"

            cid = s.get("container_id", "")[:12]
            st = stats.get(cid, {})
            cpu = st.get("cpu_percent", st.get("CPU", "—"))
            mem = st.get("mem_usage", st.get("MemUsage", "—"))

            disk = _workspace_size(s.get("workspace_path", ""))
            created = s.get("created_at", "")[:16]

            table.add_row(
                status_str,
                s.get("id", "?"),
                s.get("name", "?"),
                s.get("profile", "?"),
                str(cpu),
                str(mem),
                disk,
                created,
            )


class LogViewer(Static):
    """Live log viewer."""

    def compose(self) -> ComposeResult:
        yield Log(id="log-widget", highlight=True, max_lines=500)

    def update_logs(self, lines: list[str]):
        log_widget = self.query_one("#log-widget", Log)
        log_widget.clear()
        for line in lines:
            log_widget.write_line(line)


# ------------------------------------------------------------------
# Main App
# ------------------------------------------------------------------


class EnclaveTUI(App):
    """Enclave interactive terminal dashboard."""

    TITLE = "🏰 Enclave"
    SUB_TITLE = "AI Agent Orchestrator"
    CSS = """
    Screen {
        background: $surface;
    }

    StatusBar {
        height: 3;
        padding: 1;
        background: $primary-background;
    }

    #sessions-dt {
        height: 1fr;
    }

    #log-widget {
        height: 1fr;
    }

    TabPane {
        padding: 0;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("s", "focus_sessions", "Sessions"),
        Binding("l", "focus_logs", "Logs"),
        Binding("x", "stop_session", "Stop Session"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield StatusBar(id="status-bar")
        with TabbedContent():
            with TabPane("Sessions", id="tab-sessions"):
                yield SessionsTable(id="sessions-panel")
            with TabPane("Logs", id="tab-logs"):
                yield LogViewer(id="log-panel")
        yield Footer()

    def on_mount(self):
        self._config = _load_config()
        self._refresh_timer = self.set_interval(5.0, self._auto_refresh)
        self._do_refresh()

    def action_refresh(self):
        self._do_refresh()

    def action_focus_sessions(self):
        tabs = self.query_one(TabbedContent)
        tabs.active = "tab-sessions"

    def action_focus_logs(self):
        tabs = self.query_one(TabbedContent)
        tabs.active = "tab-logs"

    def _do_refresh(self):
        sessions = _get_sessions(self._config)
        active = [s for s in sessions if s.get("status") == "running"]
        svc_active, svc_since, svc_mem = _systemd_status()
        stats = _container_stats_all() if active else {}

        # Update status bar
        status_bar = self.query_one("#status-bar", StatusBar)
        status_bar.update_status(svc_active, svc_since, svc_mem, len(sessions), len(active))

        # Update sessions table
        sessions_panel = self.query_one("#sessions-panel", SessionsTable)
        sessions_panel.update_sessions(sessions, stats)

        # Update logs
        log_panel = self.query_one("#log-panel", LogViewer)
        log_panel.update_logs(_get_journal_lines(100))

    def _auto_refresh(self):
        self._do_refresh()

    def action_stop_session(self):
        """Stop the currently selected session."""
        table = self.query_one("#sessions-dt", DataTable)
        if table.cursor_row is None:
            self.notify("No session selected", severity="warning")
            return

        sessions = _get_sessions(self._config)
        if table.cursor_row >= len(sessions):
            return

        session = sessions[table.cursor_row]
        sid = session.get("id", "?")
        status = session.get("status", "")

        if status != "running":
            self.notify(f"Session {sid} is already {status}", severity="warning")
            return

        container_id = session.get("container_id", "")
        host_pid = session.get("host_pid")

        success = False
        if container_id:
            try:
                result = subprocess.run(
                    ["podman", "stop", container_id],
                    capture_output=True, text=True, timeout=30,
                )
                success = result.returncode == 0
            except (subprocess.TimeoutExpired, OSError):
                pass

        # Try by container name (matches session ID)
        if not success:
            try:
                result = subprocess.run(
                    ["podman", "stop", sid],
                    capture_output=True, text=True, timeout=30,
                )
                success = result.returncode == 0
            except (subprocess.TimeoutExpired, OSError):
                pass

        if not success and host_pid:
            import signal
            try:
                os.kill(host_pid, signal.SIGTERM)
                success = True
            except (ProcessLookupError, PermissionError):
                pass

        if success:
            _mark_session_stopped(self._config, sid)
            self.notify(f"Stopped session {sid}", severity="information")
        else:
            self.notify(f"Failed to stop session {sid}", severity="error")

        self._do_refresh()


def run_tui():
    """Entry point for the interactive TUI."""
    app = EnclaveTUI()
    app.run()


if __name__ == "__main__":
    run_tui()
