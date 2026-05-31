"""Tests for the in-memory heartbeat registry."""
import time
from core.framework.heartbeat import pulse, get_all, get_all_intervals, register_interval, stale_tasks, reset


class TestHeartbeat:
    def setup_method(self):
        reset()

    def test_pulse_records_timestamp(self):
        pulse("mining_tick")
        all_beats = get_all()
        assert "mining_tick" in all_beats
        assert abs(all_beats["mining_tick"] - time.time()) < 1.0

    def test_pulse_updates_existing(self):
        pulse("mining_tick")
        t1 = get_all()["mining_tick"]
        time.sleep(0.01)
        pulse("mining_tick")
        t2 = get_all()["mining_tick"]
        assert t2 > t1

    def test_get_all_returns_copy(self):
        pulse("a")
        pulse("b")
        d = get_all()
        assert len(d) == 2
        d["c"] = 999  # mutating copy shouldn't affect registry
        assert "c" not in get_all()

    def test_stale_tasks_empty_when_fresh(self):
        pulse("mining_tick")
        assert stale_tasks(max_age=60) == []

    def test_stale_tasks_returns_old(self):
        from core.framework import heartbeat
        heartbeat._registry["old_task"] = time.time() - 999
        pulse("fresh_task")
        stale = stale_tasks(max_age=60)
        assert "old_task" in stale
        assert "fresh_task" not in stale

    def test_stale_tasks_includes_never_pulsed(self):
        from core.framework import heartbeat
        heartbeat._expected.add("expected_task")
        stale = stale_tasks(max_age=60)
        assert "expected_task" in stale

    def test_expect_registers_task(self):
        from core.framework.heartbeat import expect
        expect("my_task")
        # Never pulsed, so it's stale
        assert "my_task" in stale_tasks(max_age=0)

    def test_register_interval_and_get(self):
        register_interval("hourly_task", 3600)
        intervals = get_all_intervals()
        assert intervals["hourly_task"] == 3600

    def test_stale_uses_interval_threshold(self):
        """A task with a long interval shouldn't be stale after just 5 minutes."""
        from core.framework import heartbeat
        register_interval("hourly_task", 3600)
        # Pulsed 10 minutes ago  -  stale by default max_age=300 but not by interval*3
        heartbeat._registry["hourly_task"] = time.time() - 600
        heartbeat._expected.add("hourly_task")
        stale = stale_tasks(max_age=300)
        assert "hourly_task" not in stale

    def test_stale_interval_task_actually_stale(self):
        """A task with a long interval IS stale when >3x overdue."""
        from core.framework import heartbeat
        register_interval("hourly_task", 3600)
        # Pulsed 4 hours ago  -  definitely stale
        heartbeat._registry["hourly_task"] = time.time() - 14400
        heartbeat._expected.add("hourly_task")
        stale = stale_tasks(max_age=300)
        assert "hourly_task" in stale
