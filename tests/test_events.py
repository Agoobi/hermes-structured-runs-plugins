"""Per-run SSE event buffer: replay, resume, keepalive, frame parsing, GC."""
from __future__ import annotations

import asyncio
import time
import unittest
from types import SimpleNamespace

from _plugin import _config, _events, _state


class RunEventLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_replay_then_live_tail_with_keepalive(self) -> None:
        orig = _config.SSE_KEEPALIVE_S
        _config.SSE_KEEPALIVE_S = 0.2
        log = _events.RunEventLog("r1")
        log.append("a", {})  # seq 1 (buffered before anyone subscribes)
        got = []

        async def consume():
            async for e in log.subscribe(after_seq=0):
                got.append((e["seq"], e["name"]))
                if e["name"] == "done":
                    break

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        log.append("b", {})       # seq 2 live
        await asyncio.sleep(0.35)  # force a keepalive
        log.append("done", {})     # seq 3
        try:
            await asyncio.wait_for(task, timeout=2)
        finally:
            _config.SSE_KEEPALIVE_S = orig

        self.assertEqual(got[0], (1, "a"))          # replay
        self.assertIn((2, "b"), got)                # live
        self.assertIn((None, "keepalive"), got)     # keepalive sentinel
        self.assertEqual(got[-1], (3, "done"))

    async def test_after_seq_skips_already_seen(self) -> None:
        log = _events.RunEventLog("r2")
        for n in ("a", "b", "c"):
            log.append(n, {})
        log.close()
        seen = [e["seq"] async for e in log.subscribe(after_seq=2)]
        self.assertEqual(seen, [3])

    async def test_close_ends_subscribers(self) -> None:
        log = _events.RunEventLog("r3")

        async def consume():
            return [e["name"] async for e in log.subscribe(after_seq=0)]

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        log.append("x", {})
        log.close()
        names = await asyncio.wait_for(task, timeout=2)
        self.assertEqual(names, ["x"])

    def test_max_events_trims_oldest(self) -> None:
        orig = _config.EVENT_LOG_MAX_EVENTS
        _config.EVENT_LOG_MAX_EVENTS = 3
        try:
            log = _events.RunEventLog("r4")
            for i in range(6):
                log.append(f"e{i}", {})
            self.assertEqual([e["name"] for e in log.events], ["e3", "e4", "e5"])
            self.assertEqual(log.events[-1]["seq"], 6)  # seq keeps counting
        finally:
            _config.EVENT_LOG_MAX_EVENTS = orig


class IngestFrameTests(unittest.TestCase):
    def test_bare_data_line_with_embedded_event_name(self) -> None:
        log = _events.RunEventLog("r")
        _events._ingest_frame(log, 'data: {"event": "tool.started", "tool": "terminal"}')
        self.assertEqual(log.events[-1]["name"], "tool.started")
        self.assertEqual(log.events[-1]["data"]["tool"], "terminal")

    def test_explicit_event_line(self) -> None:
        log = _events.RunEventLog("r")
        _events._ingest_frame(log, 'event: structured.completed\ndata: {"parsed": {"ok": true}}')
        self.assertEqual(log.events[-1]["name"], "structured.completed")

    def test_comment_frame_is_ignored(self) -> None:
        log = _events.RunEventLog("r")
        _events._ingest_frame(log, ": keepalive")
        _events._ingest_frame(log, ": stream closed")
        self.assertEqual(log.events, [])

    def test_non_json_data_kept_as_raw(self) -> None:
        log = _events.RunEventLog("r")
        _events._ingest_frame(log, "data: not json here")
        self.assertEqual(log.events[-1]["data"], {"raw": "not json here"})


class GcLogsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig = (dict(_events._logs), _config.EVENT_LOG_TTL_S, _config.EVENT_LOG_MAX_RUNS)
        _events._logs.clear()

    def tearDown(self) -> None:
        _events._logs.clear()
        _events._logs.update(self._orig[0])
        _config.EVENT_LOG_TTL_S, _config.EVENT_LOG_MAX_RUNS = self._orig[1], self._orig[2]

    def test_drops_closed_logs_past_ttl_and_over_cap(self) -> None:
        _config.EVENT_LOG_TTL_S = 100
        _config.EVENT_LOG_MAX_RUNS = 2
        now = time.time()
        for i in range(4):
            lg = _events.RunEventLog(f"old{i}")
            lg.closed = True
            lg.closed_at = now - 500  # past TTL
            _events._logs[f"old{i}"] = lg
        fresh = _events.RunEventLog("fresh")  # open, must survive
        _events._logs["fresh"] = fresh

        _events._gc_logs()
        self.assertEqual(set(_events._logs), {"fresh"})


class FinalizeTerminalTests(unittest.IsolatedAsyncioTestCase):
    async def test_appends_structured_event_from_finalizer(self) -> None:
        orig = (_events.finalize.finalize_structured, _state.runs)
        _state.runs = {"rF": {"run_id": "rF", "json_schema": {"type": "object"}, "structured_status": "pending"}}

        async def fake_finalize(llm, run_id, upstream, headers):
            return {"status": "completed", "structured_status": "completed", "parsed": {"ok": True}}

        _events.finalize.finalize_structured = fake_finalize
        try:
            log = _events.RunEventLog("rF")
            evt = await _events._finalize_terminal(log, {"status": "completed"}, {}, llm=SimpleNamespace())
            self.assertEqual(evt["name"], "structured.completed")
            self.assertEqual(evt["data"]["parsed"], {"ok": True})
            self.assertTrue(log.final_appended)
        finally:
            _events.finalize.finalize_structured, _state.runs = orig


if __name__ == "__main__":
    unittest.main()
