"""Tests for the cost/token tracking system."""

from __future__ import annotations

from pathlib import Path

import pytest

from enclave.common.cost_tracker import CostTracker


@pytest.fixture
def tracker(tmp_path: Path) -> CostTracker:
    """Create a CostTracker using a temp directory."""
    return CostTracker(str(tmp_path))


class TestCostTracker:
    """Tests for CostTracker."""

    def test_creates_database(self, tmp_path: Path) -> None:
        tracker = CostTracker(str(tmp_path))
        assert (tmp_path / "cost_tracking.db").exists()
        tracker.close()

    def test_record_usage(self, tracker: CostTracker) -> None:
        event = tracker.record_usage(
            session_id="s1", input_tokens=1000, output_tokens=200,
        )
        assert event["session_id"] == "s1"
        assert event["input_tokens"] == 1000
        assert event["output_tokens"] == 200
        assert event["total_tokens"] == 1200

    def test_session_stats(self, tracker: CostTracker) -> None:
        tracker.record_usage("s1", input_tokens=1000, output_tokens=200)
        tracker.record_usage("s1", input_tokens=500, output_tokens=100)

        stats = tracker.session_stats("s1")
        assert stats["total_input_tokens"] == 1500
        assert stats["total_output_tokens"] == 300
        assert stats["total_tokens"] == 1800
        assert stats["turn_count"] == 2
        assert stats["estimated_cost_usd"] > 0

    def test_session_stats_empty(self, tracker: CostTracker) -> None:
        stats = tracker.session_stats("nonexistent")
        assert stats["total_tokens"] == 0
        assert stats["turn_count"] == 0

    def test_global_stats(self, tracker: CostTracker) -> None:
        tracker.record_usage("s1", input_tokens=1000, output_tokens=200)
        tracker.record_usage("s2", input_tokens=500, output_tokens=100)

        stats = tracker.global_stats()
        assert stats["total_input_tokens"] == 1500
        assert stats["total_output_tokens"] == 300
        assert stats["session_count"] == 2
        assert stats["turn_count"] == 2

    def test_budget_not_set(self, tracker: CostTracker) -> None:
        result = tracker.check_budget("s1")
        assert result is None

    def test_budget_within_limits(self, tracker: CostTracker) -> None:
        tracker.set_budget("s1", max_tokens=10000)
        tracker.record_usage("s1", input_tokens=1000, output_tokens=200)

        budget = tracker.check_budget("s1")
        assert budget is not None
        assert budget["used_tokens"] == 1200
        assert budget["max_tokens"] == 10000
        assert not budget["over_budget"]
        assert not budget["alert"]

    def test_budget_alert_threshold(self, tracker: CostTracker) -> None:
        tracker.set_budget("s1", max_tokens=1000, alert_threshold=0.8)
        tracker.record_usage("s1", input_tokens=800, output_tokens=100)

        budget = tracker.check_budget("s1")
        assert budget is not None
        assert budget["alert"]
        assert not budget["over_budget"]

    def test_budget_exceeded(self, tracker: CostTracker) -> None:
        tracker.set_budget("s1", max_tokens=1000)
        tracker.record_usage("s1", input_tokens=800, output_tokens=300)

        budget = tracker.check_budget("s1")
        assert budget is not None
        assert budget["over_budget"]

    def test_recent_usage(self, tracker: CostTracker) -> None:
        for i in range(5):
            tracker.record_usage("s1", input_tokens=100 * (i + 1), output_tokens=10)

        recent = tracker.recent_usage("s1", limit=3)
        assert len(recent) == 3
        # Most recent first
        assert recent[0]["input_tokens"] == 500

    def test_cost_estimation(self, tracker: CostTracker) -> None:
        # 1M input tokens @ $3, 1M output tokens @ $15
        tracker.record_usage("s1", input_tokens=1_000_000, output_tokens=1_000_000)
        stats = tracker.session_stats("s1")
        assert stats["estimated_cost_usd"] == 18.0

    def test_multiple_sessions_isolated(self, tracker: CostTracker) -> None:
        tracker.record_usage("s1", input_tokens=1000, output_tokens=100)
        tracker.record_usage("s2", input_tokens=2000, output_tokens=200)

        s1 = tracker.session_stats("s1")
        s2 = tracker.session_stats("s2")
        assert s1["total_input_tokens"] == 1000
        assert s2["total_input_tokens"] == 2000

    def test_model_recorded(self, tracker: CostTracker) -> None:
        tracker.record_usage("s1", input_tokens=100, model="gpt-4o")
        recent = tracker.recent_usage("s1")
        assert recent[0]["model"] == "gpt-4o"

    def test_close_and_reopen(self, tmp_path: Path) -> None:
        tracker = CostTracker(str(tmp_path))
        tracker.record_usage("s1", input_tokens=100)
        tracker.close()
        # Re-opening should work and retain data
        tracker2 = CostTracker(str(tmp_path))
        stats = tracker2.session_stats("s1")
        assert stats["total_input_tokens"] == 100
        tracker2.close()
