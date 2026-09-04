"""STRUCTURED_RUNS_FINAL_CHECK_MODE: when the post-completion agent re-check runs.

auto   -> re-check only when the first-pass finalizer output is not schema-valid
always -> re-check every time (legacy)
off    -> never re-check; first-pass result is final
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from _plugin import _config, _finalize, _session_db, _state, _upstream


def _fake_llm(parsed):
    calls = {"n": 0}

    def complete_structured(**kwargs):
        calls["n"] += 1
        return SimpleNamespace(
            parsed=parsed,
            text="raw",
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
            _session_db.wait_for_session_settle,
            _session_db.latest_session_output,
            _finalize.post_completion_final_check,
            _upstream.json_request,
        )
        self._tmp = tempfile.TemporaryDirectory()
        _config.STATE_FILE = Path(self._tmp.name) / "s.json"
        _state.runs = {}
        _state.save_state = lambda: None

        async def _no_settle(rid):
            return {"status": "unavailable"}

        _session_db.wait_for_session_settle = _no_settle
        _session_db.latest_session_output = lambda rid: None

        self.check_calls = {"n": 0}

        async def _spy_check(rid, us, sc, hd, va=None):
            self.check_calls["n"] += 1
            return "CORRECTED OUTPUT", {"status": "completed", "run_id": "chk"}

        _finalize.post_completion_final_check = _spy_check

    def tearDown(self) -> None:
        (
            _state.runs,
            _state.save_state,
            _config.STATE_FILE,
            _config.FINAL_CHECK_MODE,
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

        self.assertEqual(self.check_calls["n"], 0)  # re-check NOT run
        self.assertEqual(llm.calls["n"], 1)  # finalizer ran once
        self.assertEqual(merged["structured_status"], "completed")
        self.assertEqual(merged["parsed"], {"summary": "ok"})
        self.assertEqual(merged["final_output_check"], {"status": "skipped", "reason": "first_pass_schema_valid"})

    async def test_auto_runs_recheck_when_first_pass_invalid(self) -> None:
        _config.FINAL_CHECK_MODE = "auto"
        self._seed("r2")
        llm = _fake_llm({"summary": 123})  # wrong type -> invalid both passes
        merged = await _finalize.finalize_structured(llm, "r2", {"status": "completed", "output": "x"}, {})

        self.assertEqual(self.check_calls["n"], 1)  # re-check ran
        self.assertEqual(llm.calls["n"], 2)  # first pass + post-check pass
        self.assertEqual(merged["structured_status"], "failed")
        self.assertEqual(merged["final_output_check"]["status"], "completed")

    async def test_always_runs_recheck_even_when_first_pass_would_be_valid(self) -> None:
        _config.FINAL_CHECK_MODE = "always"
        self._seed("r3")
        llm = _fake_llm({"summary": "ok"})
        merged = await _finalize.finalize_structured(llm, "r3", {"status": "completed", "output": "x"}, {})

        self.assertEqual(self.check_calls["n"], 1)
        self.assertEqual(llm.calls["n"], 1)  # only the post-check pass, no first pass
        self.assertEqual(merged["structured_status"], "completed")
        self.assertEqual(merged["final_output_check"]["status"], "completed")

    async def test_off_never_runs_recheck_even_when_first_pass_invalid(self) -> None:
        _config.FINAL_CHECK_MODE = "off"
        self._seed("r4")
        llm = _fake_llm({"summary": 123})  # invalid
        merged = await _finalize.finalize_structured(llm, "r4", {"status": "completed", "output": "x"}, {})

        self.assertEqual(self.check_calls["n"], 0)
        self.assertEqual(llm.calls["n"], 1)
        self.assertEqual(merged["structured_status"], "failed")  # committed as-is
        self.assertEqual(merged["final_output_check"], {"status": "skipped", "reason": "final_check_disabled"})


if __name__ == "__main__":
    unittest.main()
