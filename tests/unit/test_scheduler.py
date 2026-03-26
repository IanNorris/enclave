"""Tests for the scheduler module."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from enclave.orchestrator.scheduler import (
    MIN_CRON_INTERVAL,
    Scheduler,
    ScheduleEntry,
    TimerEntry,
)


@pytest.fixture
def tmp_scheduler(tmp_path: Path) -> Scheduler:
    """Create a scheduler with temp storage and no callbacks."""
    return Scheduler(data_dir=str(tmp_path))


class TestScheduleManagement:
    """Tests for adding and cancelling recurring schedules."""

    def test_add_schedule_valid(self, tmp_scheduler: Scheduler) -> None:
        result = tmp_scheduler.add_schedule(
            schedule_id="s1",
            session_id="sess-abc",
            interval_seconds=7200,
            reason="Check build",
        )
        assert isinstance(result, ScheduleEntry)
        assert result.id == "s1"
        assert result.interval_seconds == 7200
        assert result.next_fire > time.time()

    def test_add_schedule_below_minimum(self, tmp_scheduler: Scheduler) -> None:
        result = tmp_scheduler.add_schedule(
            schedule_id="s2",
            session_id="sess-abc",
            interval_seconds=60,  # 1 minute — too short
            reason="Too frequent",
        )
        assert isinstance(result, str)
        assert "Minimum interval" in result

    def test_add_schedule_exactly_minimum(self, tmp_scheduler: Scheduler) -> None:
        result = tmp_scheduler.add_schedule(
            schedule_id="s3",
            session_id="sess-abc",
            interval_seconds=MIN_CRON_INTERVAL,
            reason="Exactly minimum",
        )
        assert isinstance(result, ScheduleEntry)

    def test_cancel_schedule(self, tmp_scheduler: Scheduler) -> None:
        tmp_scheduler.add_schedule("s1", "sess", 7200, "test")
        assert tmp_scheduler.cancel_schedule("s1") is True
        assert tmp_scheduler.cancel_schedule("s1") is False  # already gone

    def test_list_schedules(self, tmp_scheduler: Scheduler) -> None:
        tmp_scheduler.add_schedule("s1", "sess-a", 7200, "test1")
        tmp_scheduler.add_schedule("s2", "sess-b", 7200, "test2")
        assert len(tmp_scheduler.list_schedules()) == 2
        assert len(tmp_scheduler.list_schedules("sess-a")) == 1

    def test_remove_session_entries(self, tmp_scheduler: Scheduler) -> None:
        tmp_scheduler.add_schedule("s1", "sess-a", 7200, "test1")
        tmp_scheduler.add_schedule("s2", "sess-a", 7200, "test2")
        tmp_scheduler.add_schedule("s3", "sess-b", 7200, "test3")
        tmp_scheduler.remove_session_entries("sess-a")
        assert len(tmp_scheduler.list_schedules()) == 1
        assert tmp_scheduler.list_schedules()[0].id == "s3"


class TestTimerManagement:
    """Tests for adding and cancelling one-shot timers."""

    def test_add_timer_valid(self, tmp_scheduler: Scheduler) -> None:
        result = tmp_scheduler.add_timer(
            timer_id="t1",
            session_id="sess-abc",
            fire_at=time.time() + 3600,
            reason="Check deploy",
        )
        assert isinstance(result, TimerEntry)
        assert result.id == "t1"

    def test_add_timer_in_past(self, tmp_scheduler: Scheduler) -> None:
        result = tmp_scheduler.add_timer(
            timer_id="t2",
            session_id="sess-abc",
            fire_at=time.time() - 100,
            reason="Too late",
        )
        assert isinstance(result, str)
        assert "future" in result

    def test_cancel_timer(self, tmp_scheduler: Scheduler) -> None:
        tmp_scheduler.add_timer("t1", "sess", time.time() + 3600, "test")
        assert tmp_scheduler.cancel_timer("t1") is True
        assert tmp_scheduler.cancel_timer("t1") is False

    def test_list_timers(self, tmp_scheduler: Scheduler) -> None:
        tmp_scheduler.add_timer("t1", "sess-a", time.time() + 3600, "a")
        tmp_scheduler.add_timer("t2", "sess-b", time.time() + 7200, "b")
        assert len(tmp_scheduler.list_timers()) == 2
        assert len(tmp_scheduler.list_timers("sess-b")) == 1


class TestPersistence:
    """Tests for save/load of scheduler state."""

    def test_schedules_persist(self, tmp_path: Path) -> None:
        s1 = Scheduler(data_dir=str(tmp_path))
        s1.add_schedule("s1", "sess", 7200, "test")
        s1.add_timer("t1", "sess", time.time() + 3600, "test")

        # Create new scheduler from same directory
        s2 = Scheduler(data_dir=str(tmp_path))
        assert len(s2.list_schedules()) == 1
        assert len(s2.list_timers()) == 1
        assert s2.list_schedules()[0].id == "s1"
        assert s2.list_timers()[0].id == "t1"

    def test_empty_directory_loads_fine(self, tmp_path: Path) -> None:
        s = Scheduler(data_dir=str(tmp_path))
        assert len(s.list_schedules()) == 0
        assert len(s.list_timers()) == 0


class TestScheduleAdvance:
    """Tests for the advance() method on ScheduleEntry."""

    def test_advance_skips_past(self) -> None:
        entry = ScheduleEntry(
            id="s1",
            session_id="sess",
            interval_seconds=3600,
            reason="test",
            next_fire=time.time() - 7200,  # 2 hours ago
        )
        entry.advance()
        # Should have advanced past now
        assert entry.next_fire > time.time()


class TestSchedulerTick:
    """Tests for the tick loop that fires callbacks."""

    @pytest.mark.asyncio
    async def test_schedule_fires_callback(self, tmp_path: Path) -> None:
        fired: list[tuple[str, ScheduleEntry]] = []

        async def on_fire(sid: str, entry: ScheduleEntry) -> None:
            fired.append((sid, entry))

        s = Scheduler(data_dir=str(tmp_path), on_schedule_fire=on_fire)
        # Add with next_fire in the past so it fires immediately
        entry = s.add_schedule("s1", "sess", MIN_CRON_INTERVAL, "test")
        assert isinstance(entry, ScheduleEntry)
        entry.next_fire = time.time() - 1  # force it to be due

        await s._tick()
        assert len(fired) == 1
        assert fired[0][0] == "sess"
        # next_fire should have advanced
        assert entry.next_fire > time.time()

    @pytest.mark.asyncio
    async def test_timer_fires_and_removed(self, tmp_path: Path) -> None:
        fired: list[tuple[str, TimerEntry]] = []

        async def on_fire(sid: str, entry: TimerEntry) -> None:
            fired.append((sid, entry))

        s = Scheduler(data_dir=str(tmp_path), on_timer_fire=on_fire)
        # Add with a future time, then move it to the past
        s.add_timer("t1", "sess", time.time() + 9999, "test")
        s._timers["t1"].fire_at = time.time() - 1

        await s._tick()
        assert len(fired) == 1
        assert len(s.list_timers()) == 0  # timer removed after firing

    @pytest.mark.asyncio
    async def test_timer_not_fired_if_future(self, tmp_path: Path) -> None:
        fired: list = []

        async def on_fire(sid: str, entry: TimerEntry) -> None:
            fired.append(entry)

        s = Scheduler(data_dir=str(tmp_path), on_timer_fire=on_fire)
        s.add_timer("t1", "sess", time.time() + 9999, "test")

        await s._tick()
        assert len(fired) == 0
        assert len(s.list_timers()) == 1  # still there
