"""A schema-valid finalizer result can still be a failed extraction.

Observed live in production: a long-article schema (`content: {"type":
"string"}`, no `minLength`) where the base run *and* the finalizer both
reported `completed`, `output` held the full ~21KB article, but every parsed
string field came back `""`. jsonschema has no way to flag that on its own --
an empty string satisfies `type: "string"`. `_looks_hollow` cross-checks the
extracted text length against the source instead, so this class of failure
gets a real `structured_status: failed` (and, in "auto" mode, a shot at the
post-completion re-check) rather than silently finalizing as "completed" with
next to nothing in it.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from _plugin import _config, _finalize, _session_db, _state, _upstream

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "content": {"type": "string"},
    },
    "required": ["title", "content"],
    "additionalProperties": False,
}

LONG_OUTPUT = "<p>Real article body.</p>" * 40  # well past HOLLOW_EXTRACTION_MIN_SOURCE_CHARS


class _FakeLLM:
    """Returns ``parsed`` on every call, tracking how many times it ran."""

    def __init__(self, parsed):
        self._parsed = parsed
        self.calls = 0

    def complete_structured(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(
            parsed=self._parsed,
            text="raw",
            content_type="json",
            model="fake-model",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2),
        )


class LooksHollowTests(unittest.TestCase):
    def test_blank_fields_against_a_long_source_are_hollow(self) -> None:
        self.assertTrue(_finalize._looks_hollow({"title": "", "content": ""}, LONG_OUTPUT))

    def test_a_substantial_extraction_is_not_hollow(self) -> None:
        parsed = {"title": "Bài viết", "content": LONG_OUTPUT}
        self.assertFalse(_finalize._looks_hollow(parsed, LONG_OUTPUT))

    def test_a_short_source_is_never_flagged_even_with_a_tiny_parsed_result(self) -> None:
        self.assertFalse(_finalize._looks_hollow({"title": "", "content": ""}, "ok"))

    def test_nested_values_are_counted_recursively(self) -> None:
        parsed = {"sections": [{"heading": "A" * 300}, {"heading": "B" * 300}]}
        self.assertFalse(_finalize._looks_hollow(parsed, LONG_OUTPUT))


class HollowExtractionFinalizeTests(unittest.IsolatedAsyncioTestCase):
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
        _config.STATE_FILE = Path(self._tmp.name) / "state.json"
        _config.FINAL_CHECK_MODE = "auto"
        _state.runs = {}
        _state.save_state = lambda: None

        async def _no_settle(run_id):
            return {"status": "unavailable"}

        _session_db.wait_for_session_settle = _no_settle
        _session_db.latest_session_output = lambda run_id: None

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

    def _seed(self, run_id):
        _state.runs[run_id] = {
            "run_id": run_id,
            "json_schema": SCHEMA,
            "schema_name": "t",
            "structured_done": False,
            "structured_status": "pending",
        }

    async def test_hollow_first_pass_escalates_to_the_post_completion_recheck(self) -> None:
        self._seed("run_hollow_escalates")
        check_calls = {"n": 0}

        async def _spy_check(run_id, upstream_status, schema, headers, verified_artifacts=None):
            check_calls["n"] += 1
            return LONG_OUTPUT, {"status": "completed", "run_id": "chk"}

        _finalize.post_completion_final_check = _spy_check
        llm = _FakeLLM({"title": "", "content": ""})  # still hollow even after the recheck

        merged = await _finalize.finalize_structured(
            llm, "run_hollow_escalates", {"status": "completed", "output": LONG_OUTPUT}, {}
        )

        self.assertEqual(check_calls["n"], 1)  # the hollow first pass was NOT trusted as "schema valid"
        self.assertEqual(llm.calls, 2)  # first pass + post-recheck pass
        self.assertEqual(merged["structured_status"], "failed")
        self.assertIn("finalizer_returned_hollow_extraction", merged["structured_error"])
        self.assertIsNone(merged["parsed"])

    async def test_a_substantial_first_pass_completes_normally_without_a_recheck(self) -> None:
        self._seed("run_real_content")
        check_calls = {"n": 0}

        async def _spy_check(run_id, upstream_status, schema, headers, verified_artifacts=None):
            check_calls["n"] += 1
            return LONG_OUTPUT, {"status": "completed", "run_id": "chk"}

        _finalize.post_completion_final_check = _spy_check
        llm = _FakeLLM({"title": "Bài viết", "content": LONG_OUTPUT})

        merged = await _finalize.finalize_structured(
            llm, "run_real_content", {"status": "completed", "output": LONG_OUTPUT}, {}
        )

        self.assertEqual(check_calls["n"], 0)  # a real extraction is trusted, no wasted re-check
        self.assertEqual(llm.calls, 1)
        self.assertEqual(merged["structured_status"], "completed")
        self.assertEqual(merged["parsed"]["content"], LONG_OUTPUT)

    async def test_off_mode_commits_a_hollow_first_pass_as_is(self) -> None:
        _config.FINAL_CHECK_MODE = "off"
        self._seed("run_off_mode")
        llm = _FakeLLM({"title": "", "content": ""})

        merged = await _finalize.finalize_structured(
            llm, "run_off_mode", {"status": "completed", "output": LONG_OUTPUT}, {}
        )

        self.assertEqual(llm.calls, 1)  # "off" never spends a second call re-checking
        self.assertEqual(merged["structured_status"], "completed")
        self.assertEqual(merged["parsed"], {"title": "", "content": ""})


if __name__ == "__main__":
    unittest.main()
