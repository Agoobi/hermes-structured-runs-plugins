"""Phase 3: bounded run registry (retention + max-tracked eviction)."""
from __future__ import annotations

import time
import unittest

from _plugin import _config, _state


class EvictRunsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig = (_state.runs, _config.RUN_RETENTION_S, _config.MAX_TRACKED_RUNS)
        self.now = time.time()

    def tearDown(self) -> None:
        (_state.runs, _config.RUN_RETENTION_S, _config.MAX_TRACKED_RUNS) = self._orig

    def test_retention_drops_only_old_finished_runs(self) -> None:
        _config.RUN_RETENTION_S = 100.0
        _config.MAX_TRACKED_RUNS = 0  # disable cap path
        _state.runs = {
            "old_done": {"structured_done": True, "structured_finished_at": self.now - 500},
            "recent_done": {"structured_done": True, "structured_finished_at": self.now - 10},
            "old_running": {"structured_status": "running", "created_at": self.now - 999},
            "old_pending": {"structured_status": "pending", "created_at": self.now - 999},
        }
        _state.evict_runs_locked()
        self.assertEqual(set(_state.runs), {"recent_done", "old_running", "old_pending"})

    def test_cap_drops_oldest_finished_first_and_keeps_inflight(self) -> None:
        _config.RUN_RETENTION_S = 0.0  # disable retention path
        _config.MAX_TRACKED_RUNS = 2
        _state.runs = {
            "done_a": {"structured_done": True, "structured_finished_at": self.now - 900},
            "done_b": {"structured_done": True, "structured_finished_at": self.now - 800},
            "done_c": {"structured_done": True, "structured_finished_at": self.now - 700},
            "inflight": {"structured_status": "running", "created_at": self.now - 5},
        }
        _state.evict_runs_locked()
        self.assertEqual(set(_state.runs), {"done_c", "inflight"})

    def test_noop_when_within_limits(self) -> None:
        _config.RUN_RETENTION_S = 1000.0
        _config.MAX_TRACKED_RUNS = 10
        _state.runs = {"a": {"structured_done": True, "structured_finished_at": self.now - 5}}
        self.assertFalse(_state.evict_runs_locked())
        self.assertEqual(set(_state.runs), {"a"})


if __name__ == "__main__":
    unittest.main()
