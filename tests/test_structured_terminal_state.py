"""A completed run must always reach a terminal structured state.

Regression cover for the "structured_status stays running forever" report: the
base run completes with a full output, but the finalizer never publishes
``parsed`` and never fails either, so the client cannot tell "still working"
from "result lost". Every exit path here -- caller disconnect, crash, hard
timeout, a claim left behind by a dead process -- must end at completed / failed.
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from _plugin import _config, _finalize, _session_db, _state, _upstream

SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}


class _FakeLLM:
    def __init__(self, parsed):
        self._parsed = parsed
        self.calls = 0

    def complete_structured(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(
            parsed=self._parsed,
            text="raw text",
            content_type="json",
            model="fake-model",
            usage=SimpleNamespace(input_tokens=1, output_tokens=2, total_tokens=3),
        )


class TerminalStateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._orig = (
            _state.runs,
            _state.save_state,
            _config.STATE_FILE,
            _config.FINALIZER_MAX_RUNTIME_S,
            _config.FINALIZER_STALE_AFTER_S,
            _config.FINALIZER_MAX_ATTEMPTS,
            _session_db.wait_for_session_settle,
            _session_db.latest_session_output,
            _finalize.post_completion_final_check,
            _upstream.json_request,
            dict(_finalize._TASKS),
        )
        self._tmp = tempfile.TemporaryDirectory()
        _config.STATE_FILE = Path(self._tmp.name) / "state.json"
        _state.runs = {}
        _state.save_state = lambda: None
        _finalize._TASKS.clear()

        async def _no_settle(run_id):
            return {"status": "unavailable"}

        async def _no_check(run_id, upstream_status, schema, headers, verified_artifacts=None):
            return _finalize.run_output_text(upstream_status), {"status": "fallback", "error": "stub"}

        _session_db.wait_for_session_settle = _no_settle
        _session_db.latest_session_output = lambda run_id: None
        _finalize.post_completion_final_check = _no_check

    def tearDown(self) -> None:
        tasks = dict(_finalize._TASKS)
        (
            _state.runs,
            _state.save_state,
            _config.STATE_FILE,
            _config.FINALIZER_MAX_RUNTIME_S,
            _config.FINALIZER_STALE_AFTER_S,
            _config.FINALIZER_MAX_ATTEMPTS,
            _session_db.wait_for_session_settle,
            _session_db.latest_session_output,
            _finalize.post_completion_final_check,
            _upstream.json_request,
            _finalize._TASKS,
        ) = self._orig
        _finalize._TASKS.update(tasks)
        self._tmp.cleanup()

    def _track(self, run_id: str, **extra) -> None:
        meta = {
            "run_id": run_id,
            "json_schema": SCHEMA,
            "schema_name": "t",
            "created_at": time.time(),
            "structured_done": False,
            "structured_status": "pending",
        }
        meta.update(extra)
        _state.runs[run_id] = meta

    async def _wait_terminal(self, run_id: str, timeout: float = 5.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            meta = _state.runs[run_id]
            if meta.get("structured_done"):
                return meta
            await asyncio.sleep(0.01)
        self.fail(f"{run_id} never reached a terminal structured state")

    async def test_caller_cancelled_mid_poll_still_finalizes(self) -> None:
        """A client hanging up mid-poll must not strand the run at "running"."""
        gate = asyncio.Event()

        async def _slow_settle(run_id):
            await gate.wait()
            return {"status": "settled"}

        _session_db.wait_for_session_settle = _slow_settle
        self._track("run_disconnect")
        llm = _FakeLLM({"summary": "kept"})

        poll = asyncio.ensure_future(
            _finalize.finalize_structured(
                llm, "run_disconnect", {"status": "completed", "output": "x"}, {}
            )
        )
        while _state.runs["run_disconnect"]["structured_status"] != "running":
            await asyncio.sleep(0.01)

        poll.cancel()  # the client went away
        with self.assertRaises(asyncio.CancelledError):
            await poll
        gate.set()

        meta = await self._wait_terminal("run_disconnect")
        self.assertEqual(meta["structured_status"], "completed")
        self.assertEqual(meta["parsed"], {"summary": "kept"})

    async def test_bounded_wait_answers_running_then_finalizer_completes(self) -> None:
        gate = asyncio.Event()

        async def _slow_settle(run_id):
            await gate.wait()
            return {"status": "settled"}

        _session_db.wait_for_session_settle = _slow_settle
        self._track("run_slow")
        llm = _FakeLLM({"summary": "later"})

        merged = await _finalize.finalize_structured(
            llm, "run_slow", {"status": "completed", "output": "x"}, {}, wait_s=0.05
        )
        self.assertEqual(merged["structured_status"], "running")
        self.assertEqual(merged["status"], "completed")
        self.assertIsNone(merged["parsed"])

        gate.set()
        meta = await self._wait_terminal("run_slow")
        self.assertEqual(meta["structured_status"], "completed")

        # A later poll collects the finished result rather than re-running it.
        merged = await _finalize.finalize_structured(
            llm, "run_slow", {"status": "completed", "output": "x"}, {}
        )
        self.assertEqual(merged["structured_status"], "completed")
        self.assertEqual(merged["parsed"], {"summary": "later"})
        self.assertEqual(llm.calls, 1)

    async def test_terminal_snapshot_is_persisted_before_any_await(self) -> None:
        """The completed output survives upstream dropping the run record."""
        gate = asyncio.Event()

        async def _slow_settle(run_id):
            await gate.wait()
            return {"status": "settled"}

        _session_db.wait_for_session_settle = _slow_settle
        self._track("run_snapshot")

        merged = await _finalize.finalize_structured(
            _FakeLLM({"summary": "s"}),
            "run_snapshot",
            {"status": "completed", "output": "the article"},
            {},
            wait_s=0.05,
        )
        self.assertEqual(merged["structured_status"], "running")
        snapshot = _state.runs["run_snapshot"]["upstream_snapshot"]
        self.assertEqual(snapshot["output"], "the article")

        gate.set()
        await self._wait_terminal("run_snapshot")

    async def test_finalizer_crash_ends_failed_not_running(self) -> None:
        async def _boom(run_id):
            raise RuntimeError("state db exploded")

        _session_db.wait_for_session_settle = _boom
        self._track("run_crash")
        logging.disable(logging.CRITICAL)  # expected traceback
        self.addCleanup(logging.disable, logging.NOTSET)

        merged = await _finalize.finalize_structured(
            _FakeLLM({"summary": "x"}), "run_crash", {"status": "completed", "output": "x"}, {}
        )
        self.assertEqual(merged["structured_status"], "failed")
        self.assertIn("state db exploded", merged["structured_error"])
        self.assertTrue(_state.runs["run_crash"]["structured_done"])

    async def test_finalizer_hard_timeout_ends_failed(self) -> None:
        async def _never(run_id):
            await asyncio.sleep(30)
            return {"status": "settled"}

        _session_db.wait_for_session_settle = _never
        _config.FINALIZER_MAX_RUNTIME_S = 0.05
        self._track("run_hang")
        logging.disable(logging.CRITICAL)  # expected error log
        self.addCleanup(logging.disable, logging.NOTSET)

        merged = await _finalize.finalize_structured(
            _FakeLLM({"summary": "x"}), "run_hang", {"status": "completed", "output": "x"}, {}
        )
        self.assertEqual(merged["structured_status"], "failed")
        self.assertEqual(merged["structured_error"], "structured_finalizer_timeout")

    async def test_stale_claim_from_a_dead_process_is_reclaimed(self) -> None:
        _config.FINALIZER_STALE_AFTER_S = 1.0
        self._track(
            "run_stale",
            structured_status="running",
            structured_started_at=time.time() - 3600,
            structured_attempts=1,
        )
        logging.disable(logging.CRITICAL)  # expected reclaim warning
        self.addCleanup(logging.disable, logging.NOTSET)

        merged = await _finalize.finalize_structured(
            _FakeLLM({"summary": "recovered"}),
            "run_stale",
            {"status": "completed", "output": "x"},
            {},
        )
        self.assertEqual(merged["structured_status"], "completed")
        self.assertEqual(merged["parsed"], {"summary": "recovered"})
        self.assertEqual(_state.runs["run_stale"]["structured_attempts"], 2)

    async def test_fresh_claim_is_not_stolen_from_a_live_finalizer(self) -> None:
        gate = asyncio.Event()

        async def _slow_settle(run_id):
            await gate.wait()
            return {"status": "settled"}

        _session_db.wait_for_session_settle = _slow_settle
        self._track("run_live")
        llm = _FakeLLM({"summary": "once"})
        completed = {"status": "completed", "output": "x"}

        first = await _finalize.finalize_structured(llm, "run_live", completed, {}, wait_s=0.05)
        second = await _finalize.finalize_structured(llm, "run_live", completed, {}, wait_s=0.05)
        self.assertEqual(first["structured_status"], "running")
        self.assertEqual(second["structured_status"], "running")
        self.assertEqual(_state.runs["run_live"]["structured_attempts"], 1)

        gate.set()
        await self._wait_terminal("run_live")
        self.assertEqual(llm.calls, 1)

    async def test_attempt_cap_fails_terminally_instead_of_looping(self) -> None:
        _config.FINALIZER_STALE_AFTER_S = 1.0
        _config.FINALIZER_MAX_ATTEMPTS = 2
        self._track(
            "run_giveup",
            structured_status="running",
            structured_started_at=time.time() - 3600,
            structured_attempts=2,
        )
        logging.disable(logging.CRITICAL)  # expected give-up warning
        self.addCleanup(logging.disable, logging.NOTSET)

        llm = _FakeLLM({"summary": "x"})
        merged = await _finalize.finalize_structured(
            llm, "run_giveup", {"status": "completed", "output": "x"}, {}
        )
        self.assertEqual(llm.calls, 0)
        self.assertEqual(merged["structured_status"], "failed")
        self.assertEqual(
            merged["structured_error"], "structured_finalizer_abandoned_after_2_attempts"
        )
        self.assertTrue(_state.runs["run_giveup"]["structured_done"])


if __name__ == "__main__":
    unittest.main()
