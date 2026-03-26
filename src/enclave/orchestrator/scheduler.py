"""Scheduler: manages cron jobs and one-shot timers for agent sessions.

Schedules persist to disk so they survive orchestrator restarts.
The scheduler checks every 60 seconds for due jobs and fires them.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Awaitable

from enclave.common.logging import get_logger

log = get_logger(__name__)

# Minimum interval for recurring schedules (seconds)
MIN_CRON_INTERVAL = 3600  # 1 hour


@dataclass
class ScheduleEntry:
    """A recurring schedule (cron-like)."""

    id: str
    session_id: str
    interval_seconds: int
    reason: str
    next_fire: float  # Unix timestamp
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def advance(self) -> None:
        """Move next_fire forward by one interval."""
        now = time.time()
        # Skip missed intervals (don't fire a backlog)
        while self.next_fire <= now:
            self.next_fire += self.interval_seconds


@dataclass
class TimerEntry:
    """A one-shot timer."""

    id: str
    session_id: str
    fire_at: float  # Unix timestamp
    reason: str
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class Scheduler:
    """Manages recurring schedules and one-shot timers."""

    def __init__(
        self,
        data_dir: str,
        on_schedule_fire: Callable[[str, ScheduleEntry], Awaitable[None]] | None = None,
        on_timer_fire: Callable[[str, TimerEntry], Awaitable[None]] | None = None,
    ):
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._schedules_file = self._data_dir / "schedules.json"
        self._timers_file = self._data_dir / "timers.json"

        self._schedules: dict[str, ScheduleEntry] = {}
        self._timers: dict[str, TimerEntry] = {}
        self._on_schedule_fire = on_schedule_fire
        self._on_timer_fire = on_timer_fire
        self._task: asyncio.Task | None = None

        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_schedule(
        self,
        schedule_id: str,
        session_id: str,
        interval_seconds: int,
        reason: str,
    ) -> ScheduleEntry | str:
        """Register a recurring schedule. Returns entry or error string."""
        if interval_seconds < MIN_CRON_INTERVAL:
            return (
                f"Minimum interval is {MIN_CRON_INTERVAL}s "
                f"({MIN_CRON_INTERVAL // 3600}h). Got {interval_seconds}s."
            )

        entry = ScheduleEntry(
            id=schedule_id,
            session_id=session_id,
            interval_seconds=interval_seconds,
            reason=reason,
            next_fire=time.time() + interval_seconds,
        )
        self._schedules[schedule_id] = entry
        self._save()
        log.info(
            "Schedule added: %s (session=%s, interval=%ds, reason=%s)",
            schedule_id, session_id, interval_seconds, reason,
        )
        return entry

    def cancel_schedule(self, schedule_id: str) -> bool:
        """Cancel a recurring schedule. Returns True if it existed."""
        if schedule_id in self._schedules:
            del self._schedules[schedule_id]
            self._save()
            log.info("Schedule cancelled: %s", schedule_id)
            return True
        return False

    def add_timer(
        self,
        timer_id: str,
        session_id: str,
        fire_at: float,
        reason: str,
    ) -> TimerEntry | str:
        """Register a one-shot timer. fire_at is a Unix timestamp."""
        if fire_at <= time.time():
            return "Timer fire time must be in the future."

        entry = TimerEntry(
            id=timer_id,
            session_id=session_id,
            fire_at=fire_at,
            reason=reason,
        )
        self._timers[timer_id] = entry
        self._save()
        log.info(
            "Timer added: %s (session=%s, fire_at=%s, reason=%s)",
            timer_id, session_id,
            datetime.fromtimestamp(fire_at, tz=timezone.utc).isoformat(),
            reason,
        )
        return entry

    def cancel_timer(self, timer_id: str) -> bool:
        """Cancel a one-shot timer. Returns True if it existed."""
        if timer_id in self._timers:
            del self._timers[timer_id]
            self._save()
            log.info("Timer cancelled: %s", timer_id)
            return True
        return False

    def list_schedules(self, session_id: str | None = None) -> list[ScheduleEntry]:
        """List schedules, optionally filtered by session."""
        entries = list(self._schedules.values())
        if session_id:
            entries = [e for e in entries if e.session_id == session_id]
        return entries

    def list_timers(self, session_id: str | None = None) -> list[TimerEntry]:
        """List timers, optionally filtered by session."""
        entries = list(self._timers.values())
        if session_id:
            entries = [e for e in entries if e.session_id == session_id]
        return entries

    def remove_session_entries(self, session_id: str) -> None:
        """Remove all schedules and timers for a session."""
        self._schedules = {
            k: v for k, v in self._schedules.items()
            if v.session_id != session_id
        }
        self._timers = {
            k: v for k, v in self._timers.items()
            if v.session_id != session_id
        }
        self._save()

    async def start(self) -> None:
        """Start the scheduler loop."""
        if self._task is None:
            self._task = asyncio.create_task(self._loop())
            log.info(
                "Scheduler started (%d schedules, %d timers)",
                len(self._schedules), len(self._timers),
            )

    async def stop(self) -> None:
        """Stop the scheduler loop."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        """Check every 60s for due schedules and timers."""
        while True:
            try:
                await asyncio.sleep(60)
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Scheduler tick error")

    async def _tick(self) -> None:
        """Fire any due schedules or timers."""
        now = time.time()

        # Recurring schedules
        for entry in list(self._schedules.values()):
            if entry.next_fire <= now:
                log.info("Firing schedule: %s (session=%s)", entry.id, entry.session_id)
                if self._on_schedule_fire:
                    try:
                        await self._on_schedule_fire(entry.session_id, entry)
                    except Exception:
                        log.exception("Schedule fire callback failed: %s", entry.id)
                entry.advance()
                self._save()

        # One-shot timers
        fired_timers: list[str] = []
        for entry in list(self._timers.values()):
            if entry.fire_at <= now:
                log.info("Firing timer: %s (session=%s)", entry.id, entry.session_id)
                if self._on_timer_fire:
                    try:
                        await self._on_timer_fire(entry.session_id, entry)
                    except Exception:
                        log.exception("Timer fire callback failed: %s", entry.id)
                fired_timers.append(entry.id)

        for tid in fired_timers:
            del self._timers[tid]
        if fired_timers:
            self._save()

    def _save(self) -> None:
        """Persist schedules and timers to disk."""
        try:
            with open(self._schedules_file, "w") as f:
                json.dump(
                    {k: asdict(v) for k, v in self._schedules.items()},
                    f, indent=2,
                )
            with open(self._timers_file, "w") as f:
                json.dump(
                    {k: asdict(v) for k, v in self._timers.items()},
                    f, indent=2,
                )
        except Exception:
            log.exception("Failed to save scheduler state")

    def _load(self) -> None:
        """Load schedules and timers from disk."""
        if self._schedules_file.exists():
            try:
                with open(self._schedules_file) as f:
                    data = json.load(f)
                for k, v in data.items():
                    self._schedules[k] = ScheduleEntry(**v)
                log.info("Loaded %d schedules from disk", len(self._schedules))
            except Exception:
                log.exception("Failed to load schedules")

        if self._timers_file.exists():
            try:
                with open(self._timers_file) as f:
                    data = json.load(f)
                for k, v in data.items():
                    self._timers[k] = TimerEntry(**v)
                log.info("Loaded %d timers from disk", len(self._timers))
            except Exception:
                log.exception("Failed to load timers")
