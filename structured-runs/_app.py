"""aiohttp application: the ``/v1/runs/structured/*`` routes on :8646.

Each handler is a thin adapter: forward to upstream with an allowlisted header
set, then layer the structured finalizer / media enrichment on top. No business
logic of the Hermes agent loop lives here.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
from typing import Any, Dict, Optional

from aiohttp import web

from . import _config as cfg
from . import _events as events
from . import _finalize as finalize
from . import _media as media
from . import _schema as schema_mod
from . import _session_db as session_db
from . import _state
from . import _upstream as upstream
from ._config import _now

logger = logging.getLogger("structured-runs")


def build_app(ctx) -> web.Application:
    app = web.Application(client_max_size=10_000_000)

    async def create_structured_run(request: web.Request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": {"message": "Invalid JSON"}}, status=400)

        schema = body.get("json_schema") or body.get("schema")
        err = schema_mod.schema_error(schema)
        if err:
            return web.json_response({"error": {"message": err, "type": "invalid_request_error"}}, status=400)

        upstream_body = dict(body)
        schema_name = upstream_body.pop("schema_name", None) or "run.finalizer"
        upstream_body.pop("json_schema", None)
        upstream_body.pop("schema", None)
        # Optional wrapper-only flags reserved for future use.
        upstream_body.pop("structured", None)

        headers = upstream.headers_from_request(request, json_body=True)
        status, data = await upstream.json_request("POST", "/v1/runs", headers=headers, body=upstream_body)
        if status >= 400:
            return web.json_response(data, status=status)

        run_id = data.get("run_id")
        if not run_id:
            return web.json_response(
                {"error": {"message": "Upstream /v1/runs did not return run_id", "upstream": data}},
                status=502,
            )

        with _state.LOCK:
            _state.runs[run_id] = {
                "run_id": run_id,
                "json_schema": schema,
                "schema_name": schema_name,
                "created_at": _now(),
                "structured_done": False,
                "structured_status": "pending",
            }
            _state.save_state()

        # Start buffering upstream SSE events from t0 so a client that connects
        # (or reconnects) to /events later still gets the full log; the drainer
        # also runs the finalizer on terminal.
        try:
            events.ensure_log(run_id, headers, ctx.llm)
        except Exception:
            logger.exception("[structured-runs] could not start event drainer for %s", run_id)

        out = dict(data)
        out.update({"structured": True, "structured_status": "pending", "structured_schema_name": schema_name})
        return web.json_response(out, status=status)

    async def poll_structured_run(request: web.Request):
        run_id = request.match_info["run_id"]
        headers = upstream.headers_from_request(request)
        status, upstream_status = await upstream.json_request("GET", f"/v1/runs/{run_id}", headers=headers)
        with _state.LOCK:
            meta = _state.runs.get(run_id)

        if status >= 400:
            # Security: never serve cached structured results when upstream auth
            # rejects the request. Cache fallback is only for retention/404-ish
            # cases after the caller has supplied a valid API key.
            if status in {401, 403}:
                return web.json_response(upstream_status, status=status)
            if meta and meta.get("upstream_snapshot"):
                cached = copy.deepcopy(meta["upstream_snapshot"])
                cached["upstream_status_unavailable"] = True
                cached["upstream_error"] = upstream_status
                if cached.get("status") == "completed" and not meta.get("structured_done"):
                    merged = await finalize.finalize_structured(ctx.llm, run_id, cached, headers)
                    return web.json_response(merged, status=200)
                return web.json_response(finalize.merge_structured(cached, meta), status=200)
            recovered = session_db.session_recovery_snapshot(run_id)
            if recovered:
                if recovered.get("status") == "completed" and meta:
                    merged = await finalize.finalize_structured(ctx.llm, run_id, recovered, headers)
                    return web.json_response(merged, status=200)
                return web.json_response(finalize.merge_structured(recovered, meta), status=200)
            return web.json_response(upstream_status, status=status)

        if not meta:
            # Unknown to wrapper: behave like upstream but mark no schema mapping.
            upstream_status = dict(upstream_status)
            upstream_status["structured"] = False
            upstream_status["structured_error"] = "schema_mapping_not_found"
            return web.json_response(upstream_status, status=status)

        if upstream_status.get("status") == "completed":
            merged = await finalize.finalize_structured(ctx.llm, run_id, upstream_status, headers)
            return web.json_response(merged, status=status)

        if upstream_status.get("status") in {"failed", "cancelled"}:
            with _state.LOCK:
                meta = _state.runs.get(run_id)
                if meta and not meta.get("structured_done"):
                    meta["structured_done"] = True
                    meta["structured_status"] = "skipped"
                    meta["structured_error"] = f"upstream_{upstream_status.get('status')}"
                    _state.save_state()

        with _state.LOCK:
            return web.json_response(
                finalize.merge_structured(upstream_status, _state.runs.get(run_id)), status=status
            )

    async def serve_structured_media(request: web.Request):
        """Serve a media artifact for a structured run.

        Security rules:
        - caller must have a valid API key (checked against upstream /v1/capabilities)
        - run_id must be known to this wrapper
        - requested path must match a ``*_path`` value already present in parsed JSON
        - resolved file must be under MEDIA_ROOTS (and not a sensitive file)
        """
        headers = upstream.headers_from_request(request)
        auth_status, auth_data = await upstream.json_request(
            "GET", "/v1/capabilities", headers=headers, timeout_s=30
        )
        if auth_status >= 400:
            return web.json_response(auth_data, status=auth_status)

        run_id = request.match_info["run_id"]
        raw_path = request.query.get("path") or ""
        with _state.LOCK:
            meta = copy.deepcopy(_state.runs.get(run_id))
        if not meta:
            return web.json_response({"error": {"message": "Structured run not found"}}, status=404)

        parsed = meta.get("parsed") or {}
        allowed_paths = set()
        if isinstance(parsed, dict):
            for key, val in parsed.items():
                if key.endswith("_path") and isinstance(val, str):
                    allowed_paths.add(val)
        if raw_path not in allowed_paths:
            return web.json_response(
                {"error": {"message": "Media path is not attached to this run"}}, status=403
            )

        resolved = media.resolve_media_path(raw_path)
        if not resolved:
            return web.json_response(
                {"error": {"message": "Media file not found or not allowed"}}, status=404
            )

        return web.FileResponse(path=resolved)

    def _sse_frame(evt: Dict[str, Any]) -> bytes:
        if evt.get("name") == "keepalive":
            return b": keepalive\n\n"
        lines = []
        if evt.get("seq") is not None:
            lines.append(f"id: {evt['seq']}")
        lines.append(f"event: {evt.get('name', 'message')}")
        lines.append(f"data: {json.dumps(evt.get('data', {}), ensure_ascii=False)}")
        return ("\n".join(lines) + "\n\n").encode("utf-8")

    async def stream_structured_events(request: web.Request):
        run_id = request.match_info["run_id"]
        headers = upstream.headers_from_request(request)
        headers.pop("Content-Type", None)

        after = 0
        for raw in (request.headers.get("Last-Event-ID"), request.query.get("after")):
            if raw and str(raw).isdigit():
                after = int(raw)

        # The drainer buffers every upstream event, replays from `after`, and
        # runs the finalizer on terminal (appending the structured.* event).
        log = events.ensure_log(run_id, headers, ctx.llm)

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)

        try:
            async for evt in log.subscribe(after_seq=after):
                await response.write(_sse_frame(evt))
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        except Exception:
            logger.exception("[structured-runs] event stream failed for %s", run_id)
        return response

    async def structured_run_event_log(request: web.Request):
        """GET /v1/runs/structured/{run_id}/events/log — buffered SSE events as
        JSON (a plain fetch, unlike the live /events stream). `?after=<seq>`
        returns only newer events."""
        run_id = request.match_info["run_id"]
        headers = upstream.headers_from_request(request)
        auth_status, auth_data = await upstream.json_request(
            "GET", "/v1/capabilities", headers=headers, timeout_s=30
        )
        if auth_status >= 400:
            return web.json_response(auth_data, status=auth_status)

        raw_after = request.query.get("after")
        after_seq = int(raw_after) if raw_after and raw_after.isdigit() else 0

        log = events.get_log(run_id)
        if log is None:
            with _state.LOCK:
                known = run_id in _state.runs
            if not known and session_db.session_recovery_snapshot(run_id) is None:
                return web.json_response({"error": {"message": "Structured run not found"}}, status=404)
            log = events.ensure_log(run_id, headers, ctx.llm)

        with _state.LOCK:
            meta = _state.runs.get(run_id)
        merged = finalize.merge_structured({}, meta) if meta else {}
        return web.json_response(
            {
                "run_id": run_id,
                "closed": log.closed,
                "upstream_state": log.upstream_state,
                "next_after": log.events[-1]["seq"] if log.events else after_seq,
                "events": [e for e in log.events if e["seq"] > after_seq],
                "structured": {
                    "structured_status": merged.get("structured_status"),
                    "parsed": merged.get("parsed"),
                    "structured_error": merged.get("structured_error"),
                    "structured_validation": merged.get("structured_validation"),
                    "final_output_check": merged.get("final_output_check"),
                },
            }
        )

    async def stop_structured_run(request: web.Request):
        run_id = request.match_info["run_id"]
        headers = upstream.headers_from_request(request, json_body=True)
        body = None
        try:
            if request.can_read_body:
                body = await request.json()
        except Exception:
            body = None
        status, data = await upstream.json_request(
            "POST", f"/v1/runs/{run_id}/stop", headers=headers, body=body or {}
        )
        if status < 400:
            with _state.LOCK:
                meta = _state.runs.get(run_id)
                if meta and not meta.get("structured_done"):
                    meta["structured_status"] = "skipped"
                    meta["structured_error"] = "upstream_stopping"
                    _state.save_state()
        return web.json_response(data, status=status)

    async def approve_structured_run(request: web.Request):
        run_id = request.match_info["run_id"]
        headers = upstream.headers_from_request(request, json_body=True)
        try:
            body = await request.json()
        except Exception:
            body = {}
        status, data = await upstream.json_request(
            "POST", f"/v1/runs/{run_id}/approval", headers=headers, body=body
        )
        return web.json_response(data, status=status)

    async def health(request: web.Request):
        with _state.LOCK:
            tracked = len(_state.runs)
            running_finalizers = sum(
                1 for r in _state.runs.values() if r.get("structured_status") == "running"
            )
        return web.json_response(
            {
                "status": "ok",
                "plugin": "structured-runs",
                "upstream": cfg.API_BASE,
                "tracked_runs": tracked,
                "running_finalizers": running_finalizers,
                "state_file": str(cfg.STATE_FILE),
                "jsonschema_validation": schema_mod.validation_available(),
            }
        )

    app.router.add_post("/v1/runs/structured", create_structured_run)
    app.router.add_get("/v1/runs/structured/{run_id}/events/log", structured_run_event_log)
    app.router.add_get("/v1/runs/structured/{run_id}/events", stream_structured_events)
    app.router.add_get("/v1/runs/structured/{run_id}/media", serve_structured_media)
    app.router.add_post("/v1/runs/structured/{run_id}/stop", stop_structured_run)
    app.router.add_post("/v1/runs/structured/{run_id}/approval", approve_structured_run)
    app.router.add_get("/v1/runs/structured/{run_id}", poll_structured_run)
    app.router.add_get("/health", health)
    return app
