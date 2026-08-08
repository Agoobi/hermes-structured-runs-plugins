"""
Plugin: Structured Runs Wrapper (production-safe)

Goal: keep Hermes /v1/runs behavior intact (real agent, real tools, real
polling/SSE/stop/approval), and add only a schema-validated finalizer.

Endpoints on :8646:
- POST /v1/runs/structured
- GET  /v1/runs/structured/{run_id}
- GET  /v1/runs/structured/{run_id}/events
- POST /v1/runs/structured/{run_id}/stop
- POST /v1/runs/structured/{run_id}/approval

The wrapper forwards to the real API server at STRUCTURED_RUNS_UPSTREAM
(default http://127.0.0.1:8642). Clients should send the same Bearer key as
for :8642. The wrapper persists schema/result metadata so poll still works
after gateway restart.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote

from aiohttp import ClientSession, ClientTimeout, web

logger = logging.getLogger(__name__)

API_BASE = os.getenv("STRUCTURED_RUNS_UPSTREAM", "http://127.0.0.1:8642").rstrip("/")
HERMES_HOME = Path(os.getenv("HERMES_HOME") or os.path.expanduser("~/.hermes"))
STATE_FILE = HERMES_HOME / "structured_runs_state.json"
MAX_OUTPUT_CHARS = int(os.getenv("STRUCTURED_RUNS_MAX_OUTPUT_CHARS", "200000"))
MEDIA_ROOTS = [
    Path(p).expanduser().resolve()
    for p in os.getenv(
        "STRUCTURED_RUNS_MEDIA_ROOTS",
        "/root/motion-graphic-templete,/root/.hermes,/tmp",
    ).split(",")
    if p.strip()
]

_TERMINAL = {"completed", "failed", "cancelled"}
_HEADER_ALLOWLIST = {
    "authorization",
    "x-hermes-session-id",
    "x-hermes-session-key",
    "idempotency-key",
    "accept",
    "user-agent",
}
_STATE_LOCK = threading.RLock()
_runs: Dict[str, Dict[str, Any]] = {}

try:
    import jsonschema  # type: ignore
except Exception:  # pragma: no cover - runtime optional
    jsonschema = None  # type: ignore


def _now() -> float:
    return time.time()


def _load_state() -> None:
    global _runs
    try:
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _runs = data.get("runs", {}) if isinstance(data.get("runs"), dict) else {}
    except Exception:
        logger.exception("[structured-runs] Failed to load state file %s", STATE_FILE)
        _runs = {}


def _save_state() -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        with _STATE_LOCK:
            # Do not persist request Authorization headers.
            serializable = {"runs": copy.deepcopy(_runs), "updated_at": _now()}
            for meta in serializable["runs"].values():
                meta.pop("headers", None)
                meta.pop("finalize_task", None)
        tmp.write_text(json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except Exception:
        logger.exception("[structured-runs] Failed to save state file %s", STATE_FILE)


def _schema_error(schema: Any) -> Optional[str]:
    if not isinstance(schema, dict):
        return "json_schema must be a JSON object"
    if schema.get("type") != "object":
        # complete_structured can handle more, but final API contracts should be objects.
        return "json_schema.type must be 'object' for structured run final output"
    if jsonschema is not None:
        try:
            jsonschema.Draft202012Validator.check_schema(schema)
        except Exception as exc:
            return f"Invalid JSON Schema: {exc}"
    return None


def _validate_parsed(parsed: Any, schema: Dict[str, Any]) -> Optional[str]:
    if parsed is None:
        return "finalizer_returned_non_json"
    if jsonschema is None:
        # Fail closed is too harsh if dependency absent, but expose the condition.
        return None
    try:
        jsonschema.validate(instance=parsed, schema=schema)
        return None
    except Exception as exc:
        return f"schema_validation_failed: {exc}"


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except Exception:
        return False


def _resolve_media_path(raw_path: str) -> Optional[Path]:
    """Resolve a user/run-produced media path without allowing path traversal.

    Absolute paths must be under MEDIA_ROOTS. Relative paths are tried under
    each MEDIA_ROOTS entry. Only existing regular files are returned.
    """
    if not raw_path or "\x00" in raw_path:
        return None
    p = Path(raw_path).expanduser()
    candidates = []
    if p.is_absolute():
        candidates.append(p.resolve())
    else:
        # Reject obvious traversal before joining.
        if any(part == ".." for part in p.parts):
            return None
        for root in MEDIA_ROOTS:
            candidates.append((root / p).resolve())

    for candidate in candidates:
        if not candidate.is_file():
            continue
        if any(_is_under(candidate, root) for root in MEDIA_ROOTS):
            return candidate
    return None


def _media_url_for(run_id: str, media_path: str) -> str:
    return f"/v1/runs/structured/{quote(run_id, safe='')}/media?path={quote(media_path, safe='')}"


def _enrich_media_urls(run_id: str, parsed: Any) -> Any:
    """Add URL fields when parsed JSON contains local media paths.

    Conservative behavior: only mutate existing *_url keys that are missing.
    This keeps additionalProperties:false safe because we don't add new keys
    unless the schema/finalizer already included them.
    """
    if not isinstance(parsed, dict):
        return parsed
    out = dict(parsed)
    candidates = [
        ("media_path", "media_url"),
        ("video_path", "video_url"),
        ("image_path", "image_url"),
        ("audio_path", "audio_url"),
    ]
    for path_key, url_key in candidates:
        path_val = out.get(path_key)
        if isinstance(path_val, str) and url_key in out and not out.get(url_key):
            if _resolve_media_path(path_val):
                out[url_key] = _media_url_for(run_id, path_val)
    # Common case from schemas that use media_path + video_url.
    if isinstance(out.get("media_path"), str) and "video_url" in out and not out.get("video_url"):
        if _resolve_media_path(out["media_path"]):
            out["video_url"] = _media_url_for(run_id, out["media_path"])
    return out


def _merge_structured(upstream: Dict[str, Any], meta: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    merged = dict(upstream)
    if not meta:
        return merged
    parsed = meta.get("parsed")
    if meta.get("run_id") and parsed is not None:
        parsed = _enrich_media_urls(meta["run_id"], parsed)
    merged.update(
        {
            "structured": True,
            "structured_status": meta.get("structured_status", "pending"),
            "parsed": parsed,
            "content_type": meta.get("content_type"),
            "structured_model": meta.get("structured_model"),
            "structured_usage": meta.get("structured_usage"),
            "structured_error": meta.get("structured_error"),
            "structured_schema_name": meta.get("schema_name"),
        }
    )
    return merged


def register(ctx):
    _load_state()
    app = web.Application(client_max_size=10_000_000)

    def _headers_from_request(request: web.Request, *, json_body: bool = False) -> Dict[str, str]:
        headers: Dict[str, str] = {}
        for key, value in request.headers.items():
            if key.lower() in _HEADER_ALLOWLIST:
                headers[key] = value
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    async def _json_request(
        method: str,
        path: str,
        *,
        headers: Dict[str, str],
        body: Optional[dict] = None,
        timeout_s: float = 600.0,
    ) -> Tuple[int, Dict[str, Any]]:
        timeout = ClientTimeout(total=timeout_s)
        async with ClientSession(timeout=timeout) as session:
            async with session.request(method, f"{API_BASE}{path}", headers=headers, json=body) as resp:
                text = await resp.text()
                try:
                    data = json.loads(text) if text else {}
                except Exception:
                    data = {"raw": text}
                return resp.status, data

    async def _finalize_structured(run_id: str, upstream_status: Dict[str, Any]) -> Dict[str, Any]:
        with _STATE_LOCK:
            meta = _runs.get(run_id)
            if not meta:
                return upstream_status
            if meta.get("structured_done"):
                return _merge_structured(upstream_status, meta)
            if upstream_status.get("status") != "completed":
                return _merge_structured(upstream_status, meta)
            if meta.get("structured_status") == "running":
                return _merge_structured(upstream_status, meta)
            meta["structured_status"] = "running"
            meta["structured_started_at"] = _now()
            _save_state()

        # Persist the upstream terminal snapshot before finalizing. Hermes keeps
        # run statuses only briefly; after gateway restart or retention expiry
        # /v1/runs/{id} may return 404 while this wrapper still has the schema
        # and final structured result. Keeping the terminal snapshot lets poll
        # remain useful and deterministic.
        with _STATE_LOCK:
            if run_id in _runs:
                _runs[run_id]["upstream_snapshot"] = copy.deepcopy(upstream_status)
                _save_state()

        output = upstream_status.get("output")
        if output is None:
            output = upstream_status.get("final_output") or upstream_status.get("result")
        if output is None:
            output = json.dumps(upstream_status, ensure_ascii=False)
        output_text = str(output)
        if len(output_text) > MAX_OUTPUT_CHARS:
            output_text = output_text[:MAX_OUTPUT_CHARS]

        schema = meta["json_schema"]
        schema_name = meta.get("schema_name") or "run.finalizer"
        logger.info("[structured-runs] Finalizing %s using schema=%s", run_id, schema_name)

        try:
            result = await asyncio.to_thread(
                ctx.llm.complete_structured,
                instructions=(
                    "Bạn là bước finalizer của Hermes /v1/runs. "
                    "Input là output cuối cùng của agent sau khi agent đã dùng tool nếu cần. "
                    "Chuyển output đó thành JSON đúng schema. "
                    "Không bịa dữ liệu ngoài output agent. Nếu thông tin không có, dùng giá trị rỗng/phù hợp schema. "
                    "Tuyệt đối không thêm field ngoài schema."
                ),
                input=[{"type": "text", "text": output_text}],
                json_schema=schema,
                schema_name=schema_name,
                purpose="structured-runs.finalizer",
                temperature=0.0,
            )
            parsed = _enrich_media_urls(run_id, result.parsed)
            validation_error = _validate_parsed(parsed, schema)
            with _STATE_LOCK:
                meta = _runs[run_id]
                meta["structured_done"] = True
                meta["structured_finished_at"] = _now()
                meta["content_type"] = result.content_type
                meta["structured_model"] = result.model
                meta["structured_usage"] = {
                    "input_tokens": result.usage.input_tokens,
                    "output_tokens": result.usage.output_tokens,
                    "total_tokens": result.usage.total_tokens,
                }
                if validation_error:
                    meta["structured_status"] = "failed"
                    meta["structured_error"] = validation_error
                    meta["parsed"] = None
                    meta["raw_structured_text"] = result.text[:1000]
                else:
                    meta["structured_status"] = "completed"
                    meta["structured_error"] = None
                    meta["parsed"] = parsed
                _save_state()
        except Exception as exc:
            with _STATE_LOCK:
                meta = _runs.get(run_id)
                if meta:
                    meta["structured_done"] = True
                    meta["structured_status"] = "failed"
                    meta["structured_error"] = str(exc)
                    meta["structured_finished_at"] = _now()
                    _save_state()
            logger.exception("[structured-runs] Finalizer exception for %s", run_id)

        with _STATE_LOCK:
            return _merge_structured(upstream_status, _runs.get(run_id))

    async def create_structured_run(request: web.Request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": {"message": "Invalid JSON"}}, status=400)

        schema = body.get("json_schema") or body.get("schema")
        err = _schema_error(schema)
        if err:
            return web.json_response({"error": {"message": err, "type": "invalid_request_error"}}, status=400)

        upstream_body = dict(body)
        schema_name = upstream_body.pop("schema_name", None) or "run.finalizer"
        upstream_body.pop("json_schema", None)
        upstream_body.pop("schema", None)
        # Optional wrapper-only flags reserved for future use.
        upstream_body.pop("structured", None)

        headers = _headers_from_request(request, json_body=True)
        status, data = await _json_request("POST", "/v1/runs", headers=headers, body=upstream_body)
        if status >= 400:
            return web.json_response(data, status=status)

        run_id = data.get("run_id")
        if not run_id:
            return web.json_response(
                {"error": {"message": "Upstream /v1/runs did not return run_id", "upstream": data}},
                status=502,
            )

        with _STATE_LOCK:
            _runs[run_id] = {
                "run_id": run_id,
                "json_schema": schema,
                "schema_name": schema_name,
                "created_at": _now(),
                "structured_done": False,
                "structured_status": "pending",
            }
            _save_state()

        out = dict(data)
        out.update({"structured": True, "structured_status": "pending", "structured_schema_name": schema_name})
        return web.json_response(out, status=status)

    async def poll_structured_run(request: web.Request):
        run_id = request.match_info["run_id"]
        headers = _headers_from_request(request)
        status, upstream = await _json_request("GET", f"/v1/runs/{run_id}", headers=headers)
        with _STATE_LOCK:
            meta = _runs.get(run_id)

        if status >= 400:
            # Security: never serve cached structured results when upstream auth
            # rejects the request. Cache fallback is only for retention/404-ish
            # cases after the caller has supplied a valid API key.
            if status in {401, 403}:
                return web.json_response(upstream, status=status)
            if meta and meta.get("upstream_snapshot"):
                cached = copy.deepcopy(meta["upstream_snapshot"])
                cached["upstream_status_unavailable"] = True
                cached["upstream_error"] = upstream
                return web.json_response(_merge_structured(cached, meta), status=200)
            return web.json_response(upstream, status=status)

        if not meta:
            # Unknown to wrapper: behave like upstream but mark no schema mapping.
            upstream = dict(upstream)
            upstream["structured"] = False
            upstream["structured_error"] = "schema_mapping_not_found"
            return web.json_response(upstream, status=status)

        if upstream.get("status") == "completed":
            merged = await _finalize_structured(run_id, upstream)
            return web.json_response(merged, status=status)

        if upstream.get("status") in {"failed", "cancelled"}:
            with _STATE_LOCK:
                meta = _runs.get(run_id)
                if meta and not meta.get("structured_done"):
                    meta["structured_done"] = True
                    meta["structured_status"] = "skipped"
                    meta["structured_error"] = f"upstream_{upstream.get('status')}"
                    _save_state()

        with _STATE_LOCK:
            return web.json_response(_merge_structured(upstream, _runs.get(run_id)), status=status)

    async def serve_structured_media(request: web.Request):
        """Serve media artifact for a structured run.

        Security rules:
        - caller must have a valid API key (checked against upstream /v1/capabilities)
        - run_id must be known to this wrapper
        - requested path must match a path value already present in parsed JSON
        - resolved file must be under MEDIA_ROOTS
        """
        headers = _headers_from_request(request)
        auth_status, auth_data = await _json_request("GET", "/v1/capabilities", headers=headers, timeout_s=30)
        if auth_status >= 400:
            return web.json_response(auth_data, status=auth_status)

        run_id = request.match_info["run_id"]
        raw_path = request.query.get("path") or ""
        with _STATE_LOCK:
            meta = copy.deepcopy(_runs.get(run_id))
        if not meta:
            return web.json_response({"error": {"message": "Structured run not found"}}, status=404)

        parsed = meta.get("parsed") or {}
        allowed_paths = set()
        if isinstance(parsed, dict):
            for key, val in parsed.items():
                if key.endswith("_path") and isinstance(val, str):
                    allowed_paths.add(val)
        if raw_path not in allowed_paths:
            return web.json_response({"error": {"message": "Media path is not attached to this run"}}, status=403)

        resolved = _resolve_media_path(raw_path)
        if not resolved:
            return web.json_response({"error": {"message": "Media file not found or not allowed"}}, status=404)

        return web.FileResponse(path=resolved)

    async def stream_structured_events(request: web.Request):
        run_id = request.match_info["run_id"]
        headers = _headers_from_request(request)
        headers.pop("Content-Type", None)

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
            timeout = ClientTimeout(total=None, sock_read=None)
            async with ClientSession(timeout=timeout) as session:
                async with session.get(f"{API_BASE}/v1/runs/{run_id}/events", headers=headers) as upstream_resp:
                    if upstream_resp.status >= 400:
                        text = await upstream_resp.text()
                        await response.write(
                            f"event: error\ndata: {json.dumps({'error': text, 'status': upstream_resp.status})}\n\n".encode()
                        )
                    else:
                        async for chunk in upstream_resp.content.iter_chunked(4096):
                            if chunk:
                                await response.write(chunk)
        except (ConnectionResetError, asyncio.CancelledError):
            return response
        except Exception as exc:
            await response.write(f"event: proxy.error\ndata: {json.dumps({'error': str(exc)})}\n\n".encode())

        # After upstream stream ends, poll status and emit structured final event.
        status, upstream = await _json_request("GET", f"/v1/runs/{run_id}", headers=_headers_from_request(request))
        if status < 400:
            merged = await _finalize_structured(run_id, upstream)
            sstatus = merged.get("structured_status")
            event_name = "structured.completed" if sstatus == "completed" else "structured.failed"
            if sstatus == "skipped":
                event_name = "structured.skipped"
            payload = {
                "run_id": run_id,
                "upstream_status": merged.get("status"),
                "structured_status": sstatus,
                "parsed": merged.get("parsed"),
                "content_type": merged.get("content_type"),
                "structured_model": merged.get("structured_model"),
                "structured_usage": merged.get("structured_usage"),
                "structured_error": merged.get("structured_error"),
            }
            await response.write(f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8"))
        else:
            await response.write(
                f"event: structured.failed\ndata: {json.dumps({'run_id': run_id, 'structured_error': upstream})}\n\n".encode()
            )
        return response

    async def stop_structured_run(request: web.Request):
        run_id = request.match_info["run_id"]
        headers = _headers_from_request(request, json_body=True)
        body = None
        try:
            if request.can_read_body:
                body = await request.json()
        except Exception:
            body = None
        status, data = await _json_request("POST", f"/v1/runs/{run_id}/stop", headers=headers, body=body or {})
        if status < 400:
            with _STATE_LOCK:
                meta = _runs.get(run_id)
                if meta and not meta.get("structured_done"):
                    meta["structured_status"] = "skipped"
                    meta["structured_error"] = "upstream_stopping"
                    _save_state()
        return web.json_response(data, status=status)

    async def approve_structured_run(request: web.Request):
        run_id = request.match_info["run_id"]
        headers = _headers_from_request(request, json_body=True)
        try:
            body = await request.json()
        except Exception:
            body = {}
        status, data = await _json_request("POST", f"/v1/runs/{run_id}/approval", headers=headers, body=body)
        return web.json_response(data, status=status)

    async def health(request: web.Request):
        with _STATE_LOCK:
            tracked = len(_runs)
            running_finalizers = sum(1 for r in _runs.values() if r.get("structured_status") == "running")
        return web.json_response(
            {
                "status": "ok",
                "plugin": "structured-runs",
                "upstream": API_BASE,
                "tracked_runs": tracked,
                "running_finalizers": running_finalizers,
                "state_file": str(STATE_FILE),
                "jsonschema_validation": bool(jsonschema is not None),
            }
        )

    app.router.add_post("/v1/runs/structured", create_structured_run)
    app.router.add_get("/v1/runs/structured/{run_id}/events", stream_structured_events)
    app.router.add_get("/v1/runs/structured/{run_id}/media", serve_structured_media)
    app.router.add_post("/v1/runs/structured/{run_id}/stop", stop_structured_run)
    app.router.add_post("/v1/runs/structured/{run_id}/approval", approve_structured_run)
    app.router.add_get("/v1/runs/structured/{run_id}", poll_structured_run)
    app.router.add_get("/health", health)

    def _run_server():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        runner = web.AppRunner(app)
        loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, "0.0.0.0", 8646)
        loop.run_until_complete(site.start())
        logger.info("[structured-runs] listening on :8646 upstream=%s state=%s", API_BASE, STATE_FILE)
        loop.run_forever()

    threading.Thread(target=_run_server, daemon=True).start()
    logger.info("[structured-runs] Plugin registered")
