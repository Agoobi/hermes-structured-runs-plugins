"""Per-run SSE event buffer + fan-out.

Hermes core exposes run events as a single ``asyncio.Queue`` per run: a second
subscriber steals events from the first, there is no replay, and the queue is
dropped the moment *any* subscriber disconnects (even mid-run). This module
makes the wrapper own exactly ONE upstream subscription per run, draining it
into a bounded in-memory buffer that any number of wrapper clients can replay
(from a sequence number) and tail.

Lifecycle:
- ``ensure_log(run_id, headers, llm)`` creates the log and starts the drainer
  (idempotent). Called from ``create_structured_run`` (capture from t0) and the
  events routes.
- the drainer holds ``GET :8642/v1/runs/{run_id}/events``; when that ends / 404s
  it polls ``/v1/runs/{run_id}`` until terminal.
- on terminal the drainer runs the finalizer once, appends the ``structured.*``
  event, and closes the log — so the structured result is produced even if the
  only client used SSE and disconnected early.
- a closed log is kept ``EVENT_LOG_TTL_S`` for late replay, then GC'd; live logs
  are capped at ``EVENT_LOG_MAX_RUNS`` (oldest closed dropped first).
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
from typing import Any, AsyncIterator, Dict, List, Optional

from aiohttp import ClientSession, ClientTimeout

from . import _config as cfg
from . import _finalize as finalize
from . import _session_db as session_db
from . import _state
from ._config import _now

logger = logging.getLogger("structured-runs")

_KEEPALIVE: Dict[str, Any] = {"seq": None, "name": "keepalive", "data": {}}


class RunEventLog:
    """Bounded buffer + fan-out for one run's SSE events."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.events: List[Dict[str, Any]] = []
        self.closed = False
        self.upstream_state = "pending"  # pending | streaming | poll_fallback | closed
        self.created_at = _now()
        self.closed_at: Optional[float] = None
        self.drainer: Optional[asyncio.Task] = None
        self.final_appended = False
        self._seq = 0
        self._subs: "set[asyncio.Queue]" = set()

    def append(self, name: str, data: Dict[str, Any]) -> Dict[str, Any]:
        self._seq += 1
        evt = {"seq": self._seq, "name": name, "data": data, "ts": _now()}
        self.events.append(evt)
        if len(self.events) > cfg.EVENT_LOG_MAX_EVENTS:
            del self.events[: len(self.events) - cfg.EVENT_LOG_MAX_EVENTS]
        if name.startswith("structured."):
            self.final_appended = True
        for q in list(self._subs):
            q.put_nowait(evt)
        return evt

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.closed_at = _now()
        self.upstream_state = "closed"
        for q in list(self._subs):
            q.put_nowait(None)

    async def subscribe(self, after_seq: int = 0) -> AsyncIterator[Dict[str, Any]]:
        """Yield buffered events with ``seq > after_seq``, then tail until closed.

        Emits ``_KEEPALIVE`` (``seq is None``) every ``SSE_KEEPALIVE_S`` idle sec.
        """
        q: "asyncio.Queue" = asyncio.Queue()
        self._subs.add(q)
        try:
            backlog = [e for e in self.events if e["seq"] > after_seq]
            last = backlog[-1]["seq"] if backlog else after_seq
            for e in backlog:
                yield e
            while not self.closed:
                try:
                    e = await asyncio.wait_for(q.get(), timeout=cfg.SSE_KEEPALIVE_S)
                except asyncio.TimeoutError:
                    yield _KEEPALIVE
                    continue
                if e is None:
                    break
                if e["seq"] > last:
                    last = e["seq"]
                    yield e
            for e in self.events:  # anything appended right at/after close
                if e["seq"] > last:
                    yield e
        finally:
            self._subs.discard(q)


_logs: Dict[str, RunEventLog] = {}


def get_log(run_id: str) -> Optional[RunEventLog]:
    return _logs.get(run_id)


def ensure_log(run_id: str, headers: Dict[str, str], llm: Any) -> RunEventLog:
    """Return the run's event log, starting the upstream drainer if needed."""
    _gc_logs()
    log = _logs.get(run_id)
    if log is None:
        log = RunEventLog(run_id)
        _logs[run_id] = log
    if log.drainer is None or log.drainer.done():
        drainer_headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}
        log.drainer = asyncio.create_task(_drain(log, drainer_headers, llm))
    return log


def drop_log(run_id: str) -> None:
    log = _logs.pop(run_id, None)
    if log and log.drainer and not log.drainer.done():
        log.drainer.cancel()


def _gc_logs() -> None:
    now = _now()
    for rid, log in list(_logs.items()):
        if log.closed and log.closed_at and now - log.closed_at > cfg.EVENT_LOG_TTL_S:
            _logs.pop(rid, None)
    if cfg.EVENT_LOG_MAX_RUNS > 0 and len(_logs) > cfg.EVENT_LOG_MAX_RUNS:
        closed = sorted(
            (rid for rid, lg in _logs.items() if lg.closed),
            key=lambda rid: _logs[rid].closed_at or 0.0,
        )
        for rid in closed[: len(_logs) - cfg.EVENT_LOG_MAX_RUNS]:
            _logs.pop(rid, None)


async def _drain(log: RunEventLog, headers: Dict[str, str], llm: Any) -> None:
    run_id = log.run_id
    try:
        try:
            timeout = ClientTimeout(total=None, sock_read=None)
            async with ClientSession(timeout=timeout) as session:
                async with session.get(
                    f"{cfg.API_BASE}/v1/runs/{run_id}/events", headers=headers
                ) as resp:
                    if resp.status >= 400:
                        text = await resp.text()
                        log.append(
                            "proxy.fallback",
                            {
                                "reason": "upstream_events_unavailable",
                                "status": resp.status,
                                "upstream_error": text,
                            },
                        )
                    else:
                        log.upstream_state = "streaming"
                        buf = b""
                        async for chunk in resp.content.iter_chunked(4096):
                            buf += chunk
                            while b"\n\n" in buf:
                                frame, buf = buf.split(b"\n\n", 1)
                                _ingest_frame(log, frame.decode("utf-8", "replace"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.append("proxy.fallback", {"reason": "upstream_events_exception", "error": str(exc)})

        await _poll_until_terminal(log, headers, llm)
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("[structured-runs] event drainer crashed for %s", run_id)
        log.close()


def _ingest_frame(log: RunEventLog, frame: str) -> None:
    """Parse one raw SSE frame from upstream into a buffered event."""
    name = "message"
    data_lines: List[str] = []
    for line in frame.splitlines():
        if line.startswith(":") or not line.strip():
            continue
        if line.startswith("event:"):
            name = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].lstrip())
    raw = "\n".join(data_lines)
    if not raw:
        return
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            data = {"value": data}
    except Exception:
        data = {"raw": raw}
    if name == "message" and isinstance(data.get("event"), str):
        name = data["event"]
    log.append(name, data)


async def _poll_until_terminal(log: RunEventLog, headers: Dict[str, str], llm: Any) -> None:
    """After the upstream stream ends, poll status until terminal, then finalize."""
    if log.closed:
        return
    log.upstream_state = "poll_fallback"
    run_id = log.run_id
    unknown_since: Optional[float] = None
    timeout = ClientTimeout(total=30)
    while not log.closed:
        try:
            async with ClientSession(timeout=timeout) as session:
                async with session.get(
                    f"{cfg.API_BASE}/v1/runs/{run_id}", headers=headers
                ) as resp:
                    status = resp.status
                    text = await resp.text()
            upstream = json.loads(text) if text else {}
            if not isinstance(upstream, dict):
                upstream = {"raw": upstream}
        except Exception as exc:
            log.append("status", {"run_id": run_id, "status": "unknown", "error": str(exc)})
            await asyncio.sleep(3)
            continue

        if status >= 400:
            if status in {401, 403}:
                log.append("structured.failed", {"run_id": run_id, "structured_error": upstream})
                log.close()
                return
            with _state.LOCK:
                meta = _state.runs.get(run_id)
                snap = (
                    copy.deepcopy(meta["upstream_snapshot"])
                    if meta and meta.get("upstream_snapshot")
                    else None
                )
            if snap:
                upstream = snap
                unknown_since = None
            else:
                recovered = session_db.session_recovery_snapshot(run_id)
                if recovered:
                    upstream = recovered
                    unknown_since = None
                else:
                    now = _now()
                    if unknown_since is None:
                        unknown_since = now
                    elif now - unknown_since >= cfg.SSE_UNKNOWN_TIMEOUT_S:
                        log.append(
                            "structured.failed",
                            {
                                "run_id": run_id,
                                "structured_status": "failed",
                                "structured_error": "run_not_found_upstream",
                            },
                        )
                        log.close()
                        return
                    log.append("status", {"run_id": run_id, "status": "unknown"})
                    await asyncio.sleep(3)
                    continue
        else:
            unknown_since = None

        run_state = upstream.get("status")
        if run_state not in cfg.TERMINAL_STATES:
            log.append(
                "status",
                {"run_id": run_id, "status": run_state, "source": log.upstream_state},
            )
            await asyncio.sleep(3)
            continue

        log.append("status", {"run_id": run_id, "status": run_state, "terminal": True})
        await _finalize_terminal(log, upstream, headers, llm)
        log.close()
        return


async def _finalize_terminal(
    log: RunEventLog, upstream_status: Dict[str, Any], headers: Dict[str, str], llm: Any
) -> Dict[str, Any]:
    """Run the finalizer once for a terminal run and append its structured.* event."""
    run_id = log.run_id
    if log.final_appended:
        for e in log.events:
            if e["name"].startswith("structured."):
                return e

    run_state = upstream_status.get("status")
    try:
        if run_state == "completed":
            merged = await finalize.finalize_structured(llm, run_id, upstream_status, headers)
        else:
            with _state.LOCK:
                meta = _state.runs.get(run_id)
                if meta and not meta.get("structured_done"):
                    meta["structured_done"] = True
                    meta["structured_status"] = "skipped"
                    meta["structured_error"] = f"upstream_{run_state or 'unknown'}"
                    meta["upstream_snapshot"] = copy.deepcopy(upstream_status)
                    _state.save_state()
                merged = finalize.merge_structured(upstream_status, meta)
    except Exception as exc:
        logger.exception("[structured-runs] finalize-from-drainer failed for %s", run_id)
        merged = {"structured_status": "failed", "structured_error": str(exc)}

    sstatus = merged.get("structured_status")
    name = {"completed": "structured.completed", "skipped": "structured.skipped"}.get(
        sstatus, "structured.failed"
    )
    payload = {
        "run_id": run_id,
        "upstream_status": merged.get("status"),
        "structured_status": sstatus,
        "parsed": merged.get("parsed"),
        "content_type": merged.get("content_type"),
        "structured_model": merged.get("structured_model"),
        "structured_usage": merged.get("structured_usage"),
        "structured_error": merged.get("structured_error"),
        "structured_validation": merged.get("structured_validation"),
        "final_output_check": merged.get("final_output_check"),
    }
    return log.append(name, payload)
