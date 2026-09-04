"""STRUCTURED_RUNS_FINAL_CHECK_MODE + the up-to-7 re-check loop.

auto   -> re-check only when the first-pass finalizer output is not schema-valid
always -> always run at least one re-check
off    -> never re-check; first-pass result is final
The loop retries up to STRUCTURED_RUNS_FINAL_CHECK_MAX_ATTEMPTS (hard cap 7) and
records every attempt in final_output_check.history.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from _plugin import _config, _finalize, _session_db, _state, _upstream


def _fake_llm(*parsed_sequence):
    """LLM whose complete_structured returns each parsed value in turn (last repeats)."""
    seq = list(parsed_sequence)
    calls = {"n": 0}

    def complete_structured(**kwargs):
        i = min(calls["n"], len(seq) - 1)
        calls["n"] += 1
        return SimpleNamespace(
            parsed=seq[i],
            text=f"raw#{calls['n']}",
            content_type="json",
            model="fake",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2),
        )

    return SimpleNamespace(complete_structured=complete_structured, calls=calls)


SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}


class FinalCheckModeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._orig = (
            _state.runs,
            _state.save_state,
            _config.STATE_FILE,
            _config.FINAL_CHECK_MODE,
            _config.FINAL_CHECK_MAX_ATTEMPTS,
            _config.FINAL_CHECK_STOP_ON_FALLBACK,
            _session_db.wait_for_session_settle,
            _session_db.latest_session_output,
            _finalize.post_completion_final_check,
            _upstream.json_request,
        )
        self._tmp = tempfile.TemporaryDirectory()
        _config.STATE_FILE = Path(self._tmp.name) / "s.json"
        _config.FINAL_CHECK_MAX_ATTEMPTS = 7
        _config.FINAL_CHECK_STOP_ON_FALLBACK = 2
        _state.runs = {}
        _state.save_state = lambda: None

        async def _no_settle(rid):
            return {"status": "unavailable"}

        _session_db.wait_for_session_settle = _no_settle
        _session_db.latest_session_output = lambda rid: None

        self.check_calls = []

        async def _spy_check(rid, us, sc, hd, va=None, *, attempt=1, prior_error=None, prior_parsed_preview=None):
            self.check_calls.append({"attempt": attempt, "prior_error": prior_error})
            return "CORRECTED OUTPUT", {"status": "completed", "run_id": f"chk{attempt}"}

        _finalize.post_completion_final_check = _spy_check

    def tearDown(self) -> None:
        (
            _state.runs,
            _state.save_state,
            _config.STATE_FILE,
            _config.FINAL_CHECK_MODE,
            _config.FINAL_CHECK_MAX_ATTEMPTS,
            _config.FINAL_CHECK_STOP_ON_FALLBACK,
            _session_db.wait_for_session_settle,
            _session_db.latest_session_output,
            _finalize.post_completion_final_check,
            _upstream.json_request,
        ) = self._orig
        self._tmp.cleanup()

    def _seed(self, rid):
        _state.runs[rid] = {
            "run_id": rid,
            "json_schema": SCHEMA,
            "schema_name": "t",
            "structured_done": False,
            "structured_status": "pending",
        }

    async def test_auto_skips_recheck_when_first_pass_valid(self) -> None:
        _config.FINAL_CHECK_MODE = "auto"
        self._seed("r1")
        llm = _fake_llm({"summary": "ok"})
        merged = await _finalize.finalize_structured(llm, "r1", {"status": "completed", "output": "x"}, {})

        self.assertEqual(self.check_calls, [])
        self.assertEqual(llm.calls["n"], 1)
        self.assertEqual(merged["structured_status"], "completed")
        self.assertEqual(merged["parsed"], {"summary": "ok"})
        fc = merged["final_output_check"]
        self.assertEqual(fc["status"], "skipped")
        self.assertEqual(fc["reason"], "first_pass_schema_valid")
        self.assertEqual(fc["recheck_attempts"], 0)
        self.assertEqual(fc["resolved_on_attempt"], 0)
        self.assertEqual([h["attempt"] for h in fc["history"]], [0])
        self.assertEqual(fc["history"][0]["kind"], "agent_output")

    async def test_auto_retries_until_valid_and_records_history(self) -> None:
        _config.FINAL_CHECK_MODE = "auto"
        self._seed("r2")
        # bad, bad, then good on the 3rd complete_structured call (= 2nd re-check)
        llm = _fake_llm({"summary": 1}, {"summary": 2}, {"summary": "fixed"})
        merged = await _finalize.finalize_structured(llm, "r2", {"status": "completed", "output": "x"}, {})

        self.assertEqual(len(self.check_calls), 2)
        self.assertEqual([c["attempt"] for c in self.check_calls], [1, 2])
        self.assertIsNotNone(self.check_calls[1]["prior_error"])  # error fed forward
        self.assertEqual(merged["structured_status"], "completed")
        self.assertEqual(merged["parsed"], {"summary": "fixed"})
        fc = merged["final_output_check"]
        self.assertEqual(fc["status"], "completed")
        self.assertEqual(fc["recheck_attempts"], 2)
        self.assertEqual(fc["resolved_on_attempt"], 2)
        self.assertEqual([h["attempt"] for h in fc["history"]], [0, 1, 2])
        self.assertEqual([h["outcome"] for h in fc["history"]], ["invalid", "invalid", "valid"])

    async def test_auto_exhausts_max_attempts_then_fails(self) -> None:
        _config.FINAL_CHECK_MODE = "auto"
        _config.FINAL_CHECK_MAX_ATTEMPTS = 3
        self._seed("r3")
        llm = _fake_llm({"summary": 1})  # always invalid
        merged = await _finalize.finalize_structured(llm, "r3", {"status": "completed", "output": "x"}, {})

        self.assertEqual(len(self.check_calls), 3)
        self.assertEqual(merged["structured_status"], "failed")
        self.assertIsNone(merged["parsed"])
        fc = merged["final_output_check"]
        self.assertEqual(fc["status"], "exhausted")
        self.assertEqual(fc["recheck_attempts"], 3)
        self.assertIsNone(fc["resolved_on_attempt"])
        self.assertEqual(len(fc["history"]), 4)  # attempt 0 + 3 re-checks

    async def test_max_attempts_hard_clamped_to_7(self) -> None:
        self.assertLessEqual(_config.FINAL_CHECK_MAX_ATTEMPTS, 7)
        import importlib
        # simulate a huge env value at import time
        orig = _config.FINAL_CHECK_MAX_ATTEMPTS
        try:
            _config.FINAL_CHECK_MAX_ATTEMPTS = max(0, min(999, _config._FINAL_CHECK_MAX_ATTEMPTS_CAP))
            self.assertEqual(_config.FINAL_CHECK_MAX_ATTEMPTS, 7)
        finally:
            _config.FINAL_CHECK_MAX_ATTEMPTS = orig

    async def test_stop_on_consecutive_fallbacks(self) -> None:
        _config.FINAL_CHECK_MODE = "auto"
        _config.FINAL_CHECK_MAX_ATTEMPTS = 7
        _config.FINAL_CHECK_STOP_ON_FALLBACK = 2
        self._seed("r4")

        async def _fallback_check(rid, us, sc, hd, va=None, *, attempt=1, prior_error=None, prior_parsed_preview=None):
            self.check_calls.append({"attempt": attempt})
            return "ORIGINAL", {"status": "fallback", "run_id": None, "error": "final_output_check_timeout"}

        _finalize.post_completion_final_check = _fallback_check
        llm = _fake_llm({"summary": 1})
        merged = await _finalize.finalize_structured(llm, "r4", {"status": "completed", "output": "x"}, {})

        self.assertEqual(len(self.check_calls), 2)  # stopped after 2 fallbacks, not 7
        fc = merged["final_output_check"]
        self.assertEqual(fc["status"], "fallback")
        self.assertEqual(merged["structured_status"], "failed")

    async def test_always_runs_at_least_one_recheck(self) -> None:
        _config.FINAL_CHECK_MODE = "always"
        self._seed("r5")
        llm = _fake_llm({"summary": "ok"})  # valid, but always mode still re-checks
        merged = await _finalize.finalize_structured(llm, "r5", {"status": "completed", "output": "x"}, {})

        self.assertEqual(len(self.check_calls), 1)
        self.assertEqual(merged["structured_status"], "completed")
        fc = merged["final_output_check"]
        self.assertEqual(fc["status"], "completed")
        self.assertEqual(fc["recheck_attempts"], 1)
        self.assertEqual(fc["resolved_on_attempt"], 1)

    async def test_off_never_rechecks(self) -> None:
        _config.FINAL_CHECK_MODE = "off"
        self._seed("r6")
        llm = _fake_llm({"summary": 1})  # invalid
        merged = await _finalize.finalize_structured(llm, "r6", {"status": "completed", "output": "x"}, {})

        self.assertEqual(self.check_calls, [])
        self.assertEqual(llm.calls["n"], 1)
        self.assertEqual(merged["structured_status"], "failed")
        fc = merged["final_output_check"]
        self.assertEqual(fc["status"], "skipped")
        self.assertEqual(fc["reason"], "final_check_disabled")
        self.assertEqual(fc["recheck_attempts"], 0)


if __name__ == "__main__":
    unittest.main()
