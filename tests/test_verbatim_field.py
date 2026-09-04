"""x-verbatim-from-output: substitute a large field's raw output directly
instead of trusting the finalizer LLM to reproduce it.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from _plugin import _config, _finalize, _schema, _session_db, _state, _upstream

ARTICLE = "<p>Real article body.</p>" * 40


class VerbatimFieldNameTests(unittest.TestCase):
    def test_no_marked_property_returns_none(self) -> None:
        schema = {"type": "object", "properties": {"title": {"type": "string"}}}
        self.assertIsNone(_schema.verbatim_field_name(schema))

    def test_one_marked_property_is_returned(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "content": {"type": "string", "x-verbatim-from-output": True},
            },
        }
        self.assertEqual(_schema.verbatim_field_name(schema), "content")

    def test_two_marked_properties_returns_none_not_a_guess(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "content": {"type": "string", "x-verbatim-from-output": True},
                "appendix": {"type": "string", "x-verbatim-from-output": True},
            },
        }
        self.assertIsNone(_schema.verbatim_field_name(schema))

    def test_truthy_non_true_value_does_not_count(self) -> None:
        # Only a literal `true`, matching the documented contract -- not "yes"/1/etc.
        schema = {"type": "object", "properties": {"content": {"type": "string", "x-verbatim-from-output": 1}}}
        self.assertIsNone(_schema.verbatim_field_name(schema))


class SchemaErrorVerbatimTests(unittest.TestCase):
    def test_single_verbatim_string_field_is_accepted(self) -> None:
        schema = {
            "type": "object",
            "properties": {"content": {"type": "string", "x-verbatim-from-output": True}},
        }
        self.assertIsNone(_schema.schema_error(schema))

    def test_two_verbatim_fields_are_rejected(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "content": {"type": "string", "x-verbatim-from-output": True},
                "appendix": {"type": "string", "x-verbatim-from-output": True},
            },
        }
        err = _schema.schema_error(schema)
        self.assertIsNotNone(err)
        self.assertIn("at most one property", err)

    def test_verbatim_on_a_non_string_field_is_rejected(self) -> None:
        schema = {
            "type": "object",
            "properties": {"tags": {"type": "array", "x-verbatim-from-output": True}},
        }
        err = _schema.schema_error(schema)
        self.assertIsNotNone(err)
        self.assertIn("requires type: 'string'", err)


class _FakeLLM:
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


class VerbatimSubstitutionFinalizeTests(unittest.IsolatedAsyncioTestCase):
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

    def _schema(self):
        return {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "content": {"type": "string", "x-verbatim-from-output": True},
            },
            "required": ["title", "content"],
            "additionalProperties": False,
        }

    def _seed(self, run_id):
        _state.runs[run_id] = {
            "run_id": run_id,
            "json_schema": self._schema(),
            "schema_name": "t",
            "structured_done": False,
            "structured_status": "pending",
        }

    async def test_a_blank_verbatim_field_is_overwritten_with_the_real_output_and_completes(self) -> None:
        self._seed("run_v1")
        # The LLM returns exactly the failure mode this exists for: blank content.
        llm = _FakeLLM({"title": "Bài viết", "content": ""})

        merged = await _finalize.finalize_structured(
            llm, "run_v1", {"status": "completed", "output": ARTICLE}, {}
        )

        self.assertEqual(llm.calls, 1)  # no re-check spent -- the field isn't the LLM's problem
        self.assertEqual(merged["structured_status"], "completed")
        self.assertEqual(merged["parsed"]["content"], ARTICLE)
        self.assertEqual(merged["parsed"]["title"], "Bài viết")
        self.assertEqual(merged["final_output_check"], {"status": "skipped", "reason": "first_pass_schema_valid"})

    async def test_a_non_blank_llm_value_for_the_verbatim_field_is_still_discarded(self) -> None:
        self._seed("run_v2")
        # Even a plausible-looking value must not survive -- the field is never the LLM's to fill.
        llm = _FakeLLM({"title": "Bài viết", "content": "a paraphrased summary, not the real article"})

        merged = await _finalize.finalize_structured(
            llm, "run_v2", {"status": "completed", "output": ARTICLE}, {}
        )

        self.assertEqual(merged["parsed"]["content"], ARTICLE)

    async def test_verbatim_substitution_uses_the_post_recheck_output_on_escalation(self) -> None:
        self._seed("run_v3")
        corrected_article = "CORRECTED " + ARTICLE

        async def _spy_check(run_id, upstream_status, schema, headers, verified_artifacts=None):
            return corrected_article, {"status": "completed", "run_id": "chk"}

        _finalize.post_completion_final_check = _spy_check

        class _SequenceLLM:
            """title is missing on the first pass (schema-invalid -> escalates),
            present on the post-recheck pass (schema-valid -> commits)."""

            def __init__(self):
                self.calls = 0

            def complete_structured(self, **kwargs):
                self.calls += 1
                parsed = {"content": "irrelevant"} if self.calls == 1 else {"title": "Fixed", "content": "still irrelevant"}
                return SimpleNamespace(
                    parsed=parsed, text="raw", content_type="json", model="fake-model",
                    usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2),
                )

        llm = _SequenceLLM()
        merged = await _finalize.finalize_structured(
            llm, "run_v3", {"status": "completed", "output": ARTICLE}, {}
        )

        self.assertEqual(llm.calls, 2)  # first pass (escalated) + post-recheck pass
        self.assertEqual(merged["structured_status"], "completed")
        self.assertEqual(merged["parsed"]["title"], "Fixed")
        # Substituted from the *recheck's* corrected output, not the stale original_output.
        self.assertEqual(merged["parsed"]["content"], corrected_article)


if __name__ == "__main__":
    unittest.main()
