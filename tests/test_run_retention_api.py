"""A tracked structured run stays retrievable after upstream forgets it.

Regression cover for the second half of the "Run not found" report: once the
base run is gone from the Hermes run registry, ``GET /v1/runs/structured/{id}``
answered ``404 run_not_found`` and ``/events`` never delivered a terminal
structured event, so a client could lose an already-finalized result.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from aiohttp.test_utils import AioHTTPTestCase

from _plugin import _app, _config, _finalize, _session_db, _state, _upstream

SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}
UPSTREAM_404 = {
    "error": {"message": "Run not found: r", "type": "invalid_request_error", "code": "run_not_found"}
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


class StructuredRunRetentionApiTests(AioHTTPTestCase):
    async def get_application(self):
        self.llm = _FakeLLM({"summary": "kept"})
        return _app.build_app(SimpleNamespace(llm=self.llm))

    async def asyncSetUp(self) -> None:
        self._orig = (
            _state.runs,
            _state.save_state,
            _config.STATE_FILE,
            _session_db.session_recovery_snapshot,
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
        _session_db.session_recovery_snapshot = lambda run_id: None

        async def _no_settle(run_id):
            return {"status": "unavailable"}

        async def _no_check(run_id, upstream_status, schema, headers, verified_artifacts=None):
            return _finalize.run_output_text(upstream_status), {"status": "fallback", "error": "stub"}

        _session_db.wait_for_session_settle = _no_settle
        _session_db.latest_session_output = lambda run_id: None
        _finalize.post_completion_final_check = _no_check
        self._upstream_reply = (404, UPSTREAM_404)

        async def _json_request(method, path, *, headers, body=None, timeout_s=600.0):
            return self._upstream_reply

        _upstream.json_request = _json_request
        await super().asyncSetUp()

    async def asyncTearDown(self) -> None:
        await super().asyncTearDown()
        tasks = dict(_finalize._TASKS)
        (
            _state.runs,
            _state.save_state,
            _config.STATE_FILE,
            _session_db.session_recovery_snapshot,
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

    async def test_completed_snapshot_is_finalized_after_upstream_forgets_the_run(self) -> None:
        self._track("r1", upstream_snapshot={"status": "completed", "output": "the article"})

        resp = await self.client.get("/v1/runs/structured/r1")
        body = await resp.json()

        self.assertEqual(resp.status, 200)
        self.assertEqual(body["structured_status"], "completed")
        self.assertEqual(body["parsed"], {"summary": "kept"})
        self.assertTrue(body["upstream_status_unavailable"])
        self.assertEqual(body["upstream_error"], UPSTREAM_404)

    async def test_finalized_run_is_served_from_the_wrapper_record(self) -> None:
        self._track(
            "r2",
            structured_done=True,
            structured_status="completed",
            parsed={"summary": "done"},
            upstream_snapshot={"status": "completed", "output": "the article"},
        )

        resp = await self.client.get("/v1/runs/structured/r2")
        body = await resp.json()

        self.assertEqual(resp.status, 200)
        self.assertEqual(body["structured_status"], "completed")
        self.assertEqual(body["parsed"], {"summary": "done"})
        self.assertEqual(body["output"], "the article")
        self.assertEqual(self.llm.calls, 0)

    async def test_tracked_run_without_snapshot_is_not_reported_as_404(self) -> None:
        self._track("r3")

        resp = await self.client.get("/v1/runs/structured/r3")
        body = await resp.json()

        self.assertEqual(resp.status, 200)
        self.assertEqual(body["status"], "unknown")
        self.assertEqual(body["last_event"], "upstream_run_record_lost")
        self.assertEqual(body["structured_status"], "pending")
        self.assertTrue(body["structured"])
        self.assertTrue(body["upstream_status_unavailable"])

    async def test_untracked_run_reports_a_distinct_expiry_code(self) -> None:
        resp = await self.client.get("/v1/runs/structured/gone")
        body = await resp.json()

        self.assertEqual(resp.status, 404)
        self.assertEqual(body["error"]["code"], "structured_run_expired")
        self.assertEqual(body["error"]["run_id"], "gone")
        self.assertEqual(body["error"]["structured_retention_s"], _config.RUN_RETENTION_S)
        self.assertEqual(body["error"]["upstream_error"], UPSTREAM_404)

    async def test_auth_failure_never_serves_a_cached_structured_result(self) -> None:
        self._track(
            "r4",
            structured_done=True,
            structured_status="completed",
            parsed={"summary": "secret"},
            upstream_snapshot={"status": "completed", "output": "the article"},
        )
        self._upstream_reply = (401, {"error": {"message": "bad key"}})

        resp = await self.client.get("/v1/runs/structured/r4")
        body = await resp.json()

        self.assertEqual(resp.status, 401)
        self.assertNotIn("parsed", body)

    async def test_events_emit_a_terminal_event_for_an_already_finalized_run(self) -> None:
        self._track(
            "r5",
            structured_done=True,
            structured_status="completed",
            parsed={"summary": "done"},
            content_type="json",
            upstream_snapshot={"status": "completed", "output": "the article"},
        )

        resp = await self.client.get("/v1/runs/structured/r5/events")
        text = await resp.text()

        self.assertEqual(resp.status, 200)
        self.assertIn("event: structured.completed", text)
        payload = json.loads(text.split("data: ", 1)[1].strip())
        self.assertEqual(payload["structured_status"], "completed")
        self.assertEqual(payload["parsed"], {"summary": "done"})
        self.assertEqual(payload["upstream_status"], "completed")

    async def test_events_emit_a_terminal_event_for_a_failed_finalizer(self) -> None:
        self._track(
            "r6",
            structured_done=True,
            structured_status="failed",
            parsed=None,
            structured_error="schema_validation_failed: nope",
        )

        resp = await self.client.get("/v1/runs/structured/r6/events")
        text = await resp.text()

        self.assertIn("event: structured.failed", text)
        payload = json.loads(text.split("data: ", 1)[1].strip())
        self.assertEqual(payload["structured_error"], "schema_validation_failed: nope")


if __name__ == "__main__":
    unittest.main()
