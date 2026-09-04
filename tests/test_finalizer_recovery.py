"""Phase 1: finalizer crash recovery and session-settle robustness."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import tempfile
import unittest
from pathlib import Path

from _plugin import _config, _session_db, _state


class RecoverInterruptedFinalizersTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_runs = _state.runs
        self._orig_save = _state.save_state
        self.saved: list = []
        _state.save_state = lambda: self.saved.append(True)

    def tearDown(self) -> None:
        _state.runs = self._orig_runs
        _state.save_state = self._orig_save

    def test_running_without_done_is_rewound_to_pending(self) -> None:
        _state.runs = {
            "run_a": {"structured_status": "running", "structured_done": False, "structured_started_at": 1.0},
            "run_b": {"structured_status": "running", "structured_done": True},
            "run_c": {"structured_status": "completed", "structured_done": True},
            "run_d": {"structured_status": "pending", "structured_done": False},
        }
        _state._recover_interrupted_finalizers()

        self.assertEqual(_state.runs["run_a"]["structured_status"], "pending")
        self.assertNotIn("structured_started_at", _state.runs["run_a"])
        self.assertEqual(_state.runs["run_b"]["structured_status"], "running")
        self.assertEqual(_state.runs["run_c"]["structured_status"], "completed")
        self.assertEqual(_state.runs["run_d"]["structured_status"], "pending")
        self.assertTrue(self.saved)

    def test_noop_when_nothing_orphaned(self) -> None:
        _state.runs = {"run_c": {"structured_status": "completed", "structured_done": True}}
        _state._recover_interrupted_finalizers()
        self.assertFalse(self.saved)


class SessionWorkStateReasonTests(unittest.TestCase):
    def test_missing_db_reports_no_state_db(self) -> None:
        orig = _config.STATE_DB
        _config.STATE_DB = Path("/nonexistent/does/not/exist/state.db")
        try:
            state = _session_db.session_work_state("run_x")
        finally:
            _config.STATE_DB = orig
        self.assertFalse(state["available"])
        self.assertEqual(state["reason"], "no_state_db")

    def test_query_failure_reports_query_failed(self) -> None:
        orig = _config.STATE_DB
        logging.disable(logging.CRITICAL)  # expected warning w/ traceback
        self.addCleanup(logging.disable, logging.NOTSET)
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "state.db"
            sqlite3.connect(db_path).close()  # exists but has no expected tables
            _config.STATE_DB = db_path
            try:
                state = _session_db.session_work_state("run_x")
            finally:
                _config.STATE_DB = orig
        self.assertFalse(state["available"])
        self.assertEqual(state["reason"], "query_failed")


class WaitForSessionSettleTests(unittest.TestCase):
    def _snapshot(self):
        return (
            _config.SESSION_SETTLE_TIMEOUT_S,
            _config.SESSION_SETTLE_POLL_INTERVAL_S,
            _config.SESSION_QUIET_S,
            _session_db.session_work_state,
        )

    def _restore(self, snapshot) -> None:
        (
            _config.SESSION_SETTLE_TIMEOUT_S,
            _config.SESSION_SETTLE_POLL_INTERVAL_S,
            _config.SESSION_QUIET_S,
            _session_db.session_work_state,
        ) = snapshot

    def test_query_failed_keeps_waiting_until_timeout(self) -> None:
        snap = self._snapshot()
        calls: list = []
        _config.SESSION_SETTLE_TIMEOUT_S = 0.3
        _config.SESSION_SETTLE_POLL_INTERVAL_S = 0.05
        _session_db.session_work_state = lambda run_id: (
            calls.append(run_id)
            or {
                "available": False,
                "reason": "query_failed",
                "pending_delegations": 0,
                "pending_delivery": 0,
                "last_activity_at": None,
            }
        )
        try:
            result = asyncio.run(_session_db.wait_for_session_settle("run_x"))
        finally:
            self._restore(snap)
        self.assertEqual(result["status"], "timeout")
        self.assertGreater(len(calls), 1)

    def test_no_state_db_returns_unavailable_fast(self) -> None:
        snap = self._snapshot()
        _config.SESSION_SETTLE_TIMEOUT_S = 5
        _config.SESSION_SETTLE_POLL_INTERVAL_S = 0.05
        _session_db.session_work_state = lambda run_id: {
            "available": False,
            "reason": "no_state_db",
            "pending_delegations": 0,
            "pending_delivery": 0,
            "last_activity_at": None,
        }
        try:
            result = asyncio.run(_session_db.wait_for_session_settle("run_x"))
        finally:
            self._restore(snap)
        self.assertEqual(result["status"], "unavailable")

    def test_settles_when_no_pending_work(self) -> None:
        snap = self._snapshot()
        _config.SESSION_SETTLE_TIMEOUT_S = 5
        _config.SESSION_SETTLE_POLL_INTERVAL_S = 0.05
        _config.SESSION_QUIET_S = 3
        _session_db.session_work_state = lambda run_id: {
            "available": True,
            "pending_delegations": 0,
            "pending_delivery": 0,
            "last_activity_at": None,
        }
        try:
            result = asyncio.run(_session_db.wait_for_session_settle("run_x"))
        finally:
            self._restore(snap)
        self.assertEqual(result["status"], "settled")


class SessionRecoverySnapshotTests(unittest.TestCase):
    def test_recovers_completed_session(self) -> None:
        orig = _config.STATE_DB
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "state.db"
            con = sqlite3.connect(db_path)
            con.executescript(
                """
                create table sessions (id text primary key, ended_at real, end_reason text,
                    message_count integer, input_tokens integer, output_tokens integer,
                    last_activity_at real, model text);
                create table messages (id integer primary key, session_id text, role text,
                    active integer, content text, finish_reason text, timestamp real);
                """
            )
            con.execute(
                "insert into sessions values (?,?,?,?,?,?,?,?)",
                ("run_r", 10.0, "done", 2, 5, 7, 9.0, "gpt-x"),
            )
            con.execute(
                "insert into messages values (?,?,?,?,?,?,?)",
                (1, "run_r", "assistant", 1, "final answer", "stop", 1.0),
            )
            con.commit()
            con.close()
            _config.STATE_DB = db_path
            try:
                snap = _session_db.session_recovery_snapshot("run_r")
            finally:
                _config.STATE_DB = orig
        self.assertIsNotNone(snap)
        self.assertEqual(snap["status"], "completed")
        self.assertEqual(snap["output"], "final answer")
        self.assertEqual(snap["usage"]["total_tokens"], 12)


if __name__ == "__main__":
    unittest.main()
