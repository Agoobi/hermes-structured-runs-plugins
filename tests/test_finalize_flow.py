"""Phase 3 follow-up: end-to-end finalizer flow across the split modules.

Exercises finalize_structured with a fake llm and a stubbed upstream client, so
the settle -> post-completion-check -> complete_structured -> validate ->
merge_structured chain is covered without a live Hermes gateway.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from _plugin import _config, _finalize, _session_db, _state, _upstream


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


class FinalizeFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._orig = (
            _state.runs,
            _state.save_state,
            _config.STATE_FILE,
            _config.SESSION_SETTLE_TIMEOUT_S,
            _session_db.wait_for_session_settle,
            _session_db.latest_session_output,
            _finalize.post_completion_final_check,
            _upstream.json_request,
        )
        self._tmp = tempfile.TemporaryDirectory()
        _config.STATE_FILE = Path(self._tmp.name) / "state.json"
        _state.runs = {}
        _state.save_state = lambda: None

        async def _no_settle(run_id):
            return {"status": "unavailable"}

        async def _no_check(run_id, upstream_status, schema, headers, verified_artifacts=None):
            return _finalize.run_output_text(upstream_status), {"status": "fallback", "error": "stub"}

        _session_db.wait_for_session_settle = _no_settle
        _session_db.latest_session_output = lambda run_id: None
        _finalize.post_completion_final_check = _no_check

    def tearDown(self) -> None:
        (
            _state.runs,
            _state.save_state,
            _config.STATE_FILE,
            _config.SESSION_SETTLE_TIMEOUT_S,
            _session_db.wait_for_session_settle,
            _session_db.latest_session_output,
            _finalize.post_completion_final_check,
            _upstream.json_request,
        ) = self._orig
        self._tmp.cleanup()

    async def test_happy_path_produces_validated_parsed_and_marks_done(self) -> None:
        schema = {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
            "additionalProperties": False,
        }
        _state.runs["run_1"] = {
            "run_id": "run_1",
            "json_schema": schema,
            "schema_name": "t",
            "structured_done": False,
            "structured_status": "pending",
        }
        llm = _FakeLLM({"summary": "done"})
        merged = await _finalize.finalize_structured(llm, "run_1", {"status": "completed", "output": "x"}, {})

        self.assertEqual(llm.calls, 1)
        self.assertEqual(merged["structured_status"], "completed")
        self.assertEqual(merged["parsed"], {"summary": "done"})
        self.assertEqual(merged["structured_validation"], "enforced")
        self.assertTrue(_state.runs["run_1"]["structured_done"])

    async def test_schema_violation_marks_failed_without_parsed(self) -> None:
        schema = {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
            "additionalProperties": False,
        }
        _state.runs["run_2"] = {
            "run_id": "run_2",
            "json_schema": schema,
            "structured_done": False,
            "structured_status": "pending",
        }
        llm = _FakeLLM({"summary": 123})  # wrong type
        merged = await _finalize.finalize_structured(llm, "run_2", {"status": "completed", "output": "x"}, {})

        self.assertEqual(merged["structured_status"], "failed")
        self.assertIsNone(merged["parsed"])
        self.assertIn("schema_validation_failed", merged["structured_error"])

    async def test_second_call_is_idempotent(self) -> None:
        schema = {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}
        _state.runs["run_3"] = {
            "run_id": "run_3",
            "json_schema": schema,
            "structured_done": False,
            "structured_status": "pending",
        }
        llm = _FakeLLM({"summary": "once"})
        completed = {"status": "completed", "output": "x"}
        await _finalize.finalize_structured(llm, "run_3", completed, {})
        await _finalize.finalize_structured(llm, "run_3", completed, {})
        self.assertEqual(llm.calls, 1)

    async def test_non_completed_upstream_is_not_finalized(self) -> None:
        _state.runs["run_4"] = {
            "run_id": "run_4",
            "json_schema": {"type": "object"},
            "structured_done": False,
            "structured_status": "pending",
        }
        llm = _FakeLLM({})
        merged = await _finalize.finalize_structured(llm, "run_4", {"status": "running"}, {})
        self.assertEqual(llm.calls, 0)
        self.assertEqual(merged["structured_status"], "pending")


if __name__ == "__main__":
    unittest.main()
