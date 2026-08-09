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
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from aiohttp import ClientSession, ClientTimeout, web

logger = logging.getLogger(__name__)

API_BASE = os.getenv("STRUCTURED_RUNS_UPSTREAM", "http://127.0.0.1:8642").rstrip("/")
HERMES_HOME = Path(os.getenv("HERMES_HOME") or os.path.expanduser("~/.hermes"))
STATE_FILE = HERMES_HOME / "structured_runs_state.json"
STATE_DB = HERMES_HOME / "state.db"
MAX_OUTPUT_CHARS = int(os.getenv("STRUCTURED_RUNS_MAX_OUTPUT_CHARS", "200000"))
FINAL_CHECK_TIMEOUT_S = float(os.getenv("STRUCTURED_RUNS_FINAL_CHECK_TIMEOUT_S", "120"))
FINAL_CHECK_POLL_INTERVAL_S = float(os.getenv("STRUCTURED_RUNS_FINAL_CHECK_POLL_INTERVAL_S", "1"))
SESSION_SETTLE_TIMEOUT_S = float(os.getenv("STRUCTURED_RUNS_SESSION_SETTLE_TIMEOUT_S", "180"))
SESSION_QUIET_S = float(os.getenv("STRUCTURED_RUNS_SESSION_QUIET_S", "3"))
SESSION_SETTLE_POLL_INTERVAL_S = float(os.getenv("STRUCTURED_RUNS_SESSION_SETTLE_POLL_INTERVAL_S", "1"))
_MEDIA_PATH_RE = re.compile(
    r"(?:MEDIA:)?(?:(?:/|~?/)?(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.(?:mp4|mov|mkv|webm|mp3|wav|m4a|ogg|png|jpe?g|webp|gif|pdf|docx|xlsx|csv|zip))",
    re.IGNORECASE,
)
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


def _session_recovery_snapshot(run_id: str) -> Optional[Dict[str, Any]]:
    """Recover an upstream-like run snapshot from Hermes session DB.

    /v1/runs status/events are an in-memory registry and can disappear while
    the persistent API session still exists. This function lets the wrapper
    finalize completed sessions and avoid false structured.failed events for
    active/interrupted sessions.
    """
    if not STATE_DB.exists():
        return None
    try:
        con = sqlite3.connect(str(STATE_DB))
        con.row_factory = sqlite3.Row
        session = con.execute(
            "select id, ended_at, end_reason, message_count, input_tokens, output_tokens, last_activity_at, model from sessions where id=?",
            (run_id,),
        ).fetchone()
        if not session:
            return None
        rows = con.execute(
            """
            select id, content, finish_reason, timestamp
            from messages
            where session_id=? and role='assistant' and active=1
              and content is not null and trim(content) != ''
            order by id desc
            limit 20
            """,
            (run_id,),
        ).fetchall()
        final_content = None
        final_message_id = None
        interrupted = False
        for row in rows:
            content = row["content"] or ""
            if content.startswith("Operation interrupted:"):
                interrupted = True
                continue
            # Ignore tool-call placeholder assistant messages.
            if row["finish_reason"] == "tool_calls":
                continue
            final_content = content
            final_message_id = row["id"]
            break

        ended = session["ended_at"] is not None
        if ended and final_content:
            return {
                "object": "hermes.run",
                "run_id": run_id,
                "status": "completed",
                "session_id": run_id,
                "model": session["model"],
                "last_event": "session.recovered",
                "output": final_content,
                "usage": {
                    "input_tokens": session["input_tokens"] or 0,
                    "output_tokens": session["output_tokens"] or 0,
                    "total_tokens": (session["input_tokens"] or 0) + (session["output_tokens"] or 0),
                },
                "session_recovered": True,
                "session_final_message_id": final_message_id,
            }

        return {
            "object": "hermes.run",
            "run_id": run_id,
            "status": "unknown",
            "session_id": run_id,
            "model": session["model"],
            "last_event": "session.active_without_run_registry",
            "session_recovered": True,
            "session_active": not ended,
            "session_interrupted": interrupted,
            "structured_error": "upstream_run_registry_lost_but_session_exists",
        }
    except Exception:
        logger.exception("[structured-runs] Failed to recover session snapshot for %s", run_id)
        return None


def _session_work_state(run_id: str) -> Dict[str, Any]:
    """Return whether delegated work or delivery is still pending for a session."""
    if not STATE_DB.exists():
        return {"available": False, "pending_delegations": 0, "pending_delivery": 0, "last_activity_at": None}
    try:
        con = sqlite3.connect(str(STATE_DB))
        try:
            session = con.execute(
                "select last_activity_at from sessions where id=?", (run_id,)
            ).fetchone()
            rows = con.execute(
                """
                select state, delivery_state
                from async_delegations
                where origin_session=? or parent_session_id=? or origin_session_id=?
                """,
                (run_id, run_id, run_id),
            ).fetchall()
            pending_delegations = sum(
                1 for state, _ in rows if state not in {"completed", "failed", "cancelled"}
            )
            pending_delivery = sum(
                1
                for state, delivery_state in rows
                if state in {"completed", "failed", "cancelled"} and delivery_state != "delivered"
            )
            return {
                "available": True,
                "pending_delegations": pending_delegations,
                "pending_delivery": pending_delivery,
                "last_activity_at": session[0] if session else None,
            }
        finally:
            con.close()
    except Exception:
        logger.debug("[structured-runs] Could not inspect delegated work for %s", run_id, exc_info=True)
        return {"available": False, "pending_delegations": 0, "pending_delivery": 0, "last_activity_at": None}


def _latest_session_output(run_id: str) -> Optional[Dict[str, Any]]:
    """Return the newest non-tool assistant reply persisted for this session."""
    if not STATE_DB.exists():
        return None
    try:
        con = sqlite3.connect(str(STATE_DB))
        con.row_factory = sqlite3.Row
        try:
            row = con.execute(
                """
                select id, content, timestamp
                from messages
                where session_id=? and role='assistant' and active=1
                  and content is not null and trim(content) != ''
                  and (finish_reason is null or finish_reason != 'tool_calls')
                order by id desc limit 1
                """,
                (run_id,),
            ).fetchone()
            if not row:
                return None
            return {"message_id": row["id"], "output": row["content"], "timestamp": row["timestamp"]}
        finally:
            con.close()
    except Exception:
        logger.debug("[structured-runs] Could not read latest output for %s", run_id, exc_info=True)
        return None


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


def _final_output_check_prompt(schema: Dict[str, Any], verified_artifacts: Optional[List[str]] = None) -> str:
    """Build a schema-aware, post-completion correction request for the agent.

    This is deliberately sent as a follow-up API run in the same session after
    the original work finishes. It does not assume a particular schema shape or
    artifact type: every client-provided JSON Schema gets the same final-output
    check, while artifact/file fields receive explicit MEDIA path guidance.
    """
    schema_text = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    prompt = (
        "BƯỚC KIỂM TRA OUTPUT CUỐI — BẮT BUỘC. Bạn vừa hoàn thành tác vụ ở lượt trước. "
        "Hãy tự rà soát câu trả lời cuối theo JSON Schema của client bên dưới, rồi trả lại "
        "MỘT câu trả lời cuối đã được sửa để finalizer có thể điền đúng mọi field bắt buộc. "
        "Không chỉ xác nhận 'đã đúng' và không mô tả việc kiểm tra. "
        "Nếu tác vụ tạo bất kỳ artifact nào (video, audio, image, PDF, DOCX, CSV, ZIP hoặc file khác), "
        "hãy xác minh file tồn tại. Nếu wrapper đã liệt kê ARTIFACT ĐÃ XÁC MINH bên dưới, "
        "hãy dùng chính absolute path đó; không chạy realpath cho relative path từ cwd khác. "
        "Trong JSON field có tên kết thúc bằng _path, dùng bare absolute path, KHÔNG thêm prefix MEDIA:. "
        "Chỉ dùng MEDIA:/absolute/path/to/file.ext ở phần text delivery ngoài JSON. "
        "Tuyệt đối không chỉ trả relative path. Nếu schema yêu cầu URL/path/file/media/artifact thì "
        "cung cấp bằng chứng rõ ràng tương ứng. Không bịa giá trị: nếu một field bắt buộc không thể "
        "cung cấp, nói rõ lý do để structured finalizer trả lỗi thay vì im lặng cho null thành công. "
        "Không thêm field ngoài schema.\n\n"
        f"JSON Schema client gửi:\n{schema_text}"
    )
    if verified_artifacts:
        prompt += "\n\nARTIFACT ĐÃ XÁC MINH BỞI WRAPPER (authoritative):\n" + "\n".join(
            f"- {path}" for path in verified_artifacts
        )
    return prompt


def _run_output_text(status: Dict[str, Any]) -> str:
    output = status.get("output")
    if output is None:
        output = status.get("final_output") or status.get("result")
    if output is None:
        output = json.dumps(status, ensure_ascii=False)
    return str(output)


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except Exception:
        return False


def _resolve_media_path(raw_path: str) -> Optional[Path]:
    """Resolve a run-produced artifact path without allowing traversal.

    `MEDIA:/...` is accepted as an input transport marker, but callers receive
    only the canonical filesystem path. Relative paths are resolved against the
    explicit allowed roots, not the gateway process cwd.
    """
    if not raw_path or "\x00" in raw_path:
        return None
    raw_path = raw_path.strip()
    if raw_path.startswith("MEDIA:"):
        raw_path = raw_path[len("MEDIA:"):]
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


def _verified_artifacts_from_text(text: str) -> List[str]:
    """Find existing artifacts mentioned in agent output and canonicalize them.

    This is deliberately evidence-based: a value is returned only if it both
    appears in the agent output and resolves to an existing regular file under
    an allowed root. It makes a relative path independent of an agent's later
    working directory.
    """
    found: List[str] = []
    for match in _MEDIA_PATH_RE.finditer(text or ""):
        resolved = _resolve_media_path(match.group(0))
        if resolved:
            value = str(resolved)
            if value not in found:
                found.append(value)
    return found


def _canonicalize_artifact_paths(parsed: Any) -> Any:
    """Normalize only existing `*_path` values to safe absolute paths."""
    if not isinstance(parsed, dict):
        return parsed
    out = dict(parsed)
    for key, value in out.items():
        if key.endswith("_path") and isinstance(value, str):
            resolved = _resolve_media_path(value)
            if resolved:
                out[key] = str(resolved)
    return out


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
            "final_output_check": meta.get("final_output_check"),
            "session_settle": meta.get("session_settle"),
            "verified_artifacts": meta.get("verified_artifacts", []),
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

    async def _wait_for_session_settle(run_id: str) -> Dict[str, Any]:
        """Wait for delegated work delivery and a brief quiet window.

        `/v1/runs` can report completed while a background `delegate_task` is
        still delivering its result to the same session. Finalizing immediately
        would capture a stale assistant reply. We wait only for the session's
        own durable delegation records, with a bounded timeout.
        """
        deadline = _now() + SESSION_SETTLE_TIMEOUT_S
        last_state: Dict[str, Any] = {}
        while _now() < deadline:
            state = _session_work_state(run_id)
            last_state = state
            last_activity = state.get("last_activity_at")
            quiet = (
                last_activity is None
                or _now() - float(last_activity) >= SESSION_QUIET_S
            )
            if (
                state.get("pending_delegations", 0) == 0
                and state.get("pending_delivery", 0) == 0
                and quiet
            ):
                return {"status": "settled", **state}
            await asyncio.sleep(SESSION_SETTLE_POLL_INTERVAL_S)
        return {"status": "timeout", **last_state}

    async def _post_completion_final_check(
        run_id: str,
        upstream_status: Dict[str, Any],
        schema: Dict[str, Any],
        headers: Dict[str, str],
        verified_artifacts: Optional[List[str]] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """Ask the completed upstream agent to produce a corrected final answer.

        The follow-up uses the same Hermes session as the original `/v1/runs`
        call. This makes the user-requested check an actual agent turn after
        completion, not merely an LLM finalizer instruction. A failed/expired
        follow-up falls back to the original output so it cannot erase a valid
        completed run.
        """
        original_output = _run_output_text(upstream_status)
        session_id = upstream_status.get("session_id") or run_id
        prompt = _final_output_check_prompt(schema, verified_artifacts)
        prompt += "\n\nCâu trả lời cuối trước khi kiểm tra:\n" + original_output[:MAX_OUTPUT_CHARS]
        followup_headers = dict(headers)
        followup_headers["Content-Type"] = "application/json"
        status, started = await _json_request(
            "POST",
            "/v1/runs",
            headers=followup_headers,
            body={"input": prompt, "session_id": session_id},
            timeout_s=30,
        )
        if status >= 400 or not started.get("run_id"):
            return original_output, {
                "status": "fallback",
                "error": f"final_output_check_start_failed: {started}",
            }

        check_run_id = str(started["run_id"])
        deadline = _now() + FINAL_CHECK_TIMEOUT_S
        while _now() < deadline:
            status, checked = await _json_request(
                "GET", f"/v1/runs/{check_run_id}", headers=headers, timeout_s=30
            )
            if status >= 400:
                return original_output, {
                    "status": "fallback",
                    "run_id": check_run_id,
                    "error": f"final_output_check_poll_failed: {checked}",
                }
            checked_status = checked.get("status")
            if checked_status == "completed":
                checked_output = _run_output_text(checked)
                if checked_output.strip():
                    return checked_output, {"status": "completed", "run_id": check_run_id}
                return original_output, {
                    "status": "fallback",
                    "run_id": check_run_id,
                    "error": "final_output_check_returned_empty_output",
                }
            if checked_status in {"failed", "cancelled"}:
                return original_output, {
                    "status": "fallback",
                    "run_id": check_run_id,
                    "error": f"final_output_check_{checked_status}",
                }
            await asyncio.sleep(FINAL_CHECK_POLL_INTERVAL_S)

        return original_output, {
            "status": "fallback",
            "run_id": check_run_id,
            "error": "final_output_check_timeout",
        }

    async def _finalize_structured(
        run_id: str, upstream_status: Dict[str, Any], headers: Dict[str, str]
    ) -> Dict[str, Any]:
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

        # A terminal /v1/runs state can precede delivery of background
        # delegation results. Wait for durable delegation delivery, then read
        # the latest persisted assistant reply rather than freezing the first
        # terminal response.
        settle = await _wait_for_session_settle(run_id)
        latest_output = _latest_session_output(run_id)
        if latest_output and latest_output.get("output"):
            upstream_status = dict(upstream_status)
            upstream_status["output"] = latest_output["output"]
            upstream_status["last_event"] = "session.settled"
            upstream_status["session_final_message_id"] = latest_output["message_id"]

        # Persist the settled terminal snapshot before finalizing. Hermes keeps
        # run statuses only briefly; after gateway restart or retention expiry
        # /v1/runs/{id} may return 404 while this wrapper still has the schema
        # and final structured result. Keeping the terminal snapshot lets poll
        # remain useful and deterministic.
        with _STATE_LOCK:
            if run_id in _runs:
                _runs[run_id]["session_settle"] = settle
                _runs[run_id]["upstream_snapshot"] = copy.deepcopy(upstream_status)
                _save_state()

        schema = meta["json_schema"]
        schema_name = meta.get("schema_name") or "run.finalizer"
        original_output = _run_output_text(upstream_status)
        verified_artifacts = _verified_artifacts_from_text(original_output)
        logger.info(
            "[structured-runs] Running post-completion output check for %s (artifacts=%d)",
            run_id,
            len(verified_artifacts),
        )
        output_text, final_check = await _post_completion_final_check(
            run_id, upstream_status, schema, headers, verified_artifacts
        )
        if verified_artifacts:
            output_text += "\n\nARTIFACT ĐÃ XÁC MINH BỞI WRAPPER:\n" + "\n".join(
                f"- {path}" for path in verified_artifacts
            )
        if len(output_text) > MAX_OUTPUT_CHARS:
            output_text = output_text[:MAX_OUTPUT_CHARS]
        with _STATE_LOCK:
            if run_id in _runs:
                _runs[run_id]["final_output_check"] = final_check
                _runs[run_id]["verified_artifacts"] = verified_artifacts
                _save_state()
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
            parsed = _canonicalize_artifact_paths(result.parsed)
            parsed = _enrich_media_urls(run_id, parsed)
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
                if cached.get("status") == "completed" and not meta.get("structured_done"):
                    merged = await _finalize_structured(run_id, cached, headers)
                    return web.json_response(merged, status=200)
                return web.json_response(_merge_structured(cached, meta), status=200)
            recovered = _session_recovery_snapshot(run_id)
            if recovered:
                if recovered.get("status") == "completed" and meta:
                    merged = await _finalize_structured(run_id, recovered, headers)
                    return web.json_response(merged, status=200)
                return web.json_response(_merge_structured(recovered, meta), status=200)
            return web.json_response(upstream, status=status)

        if not meta:
            # Unknown to wrapper: behave like upstream but mark no schema mapping.
            upstream = dict(upstream)
            upstream["structured"] = False
            upstream["structured_error"] = "schema_mapping_not_found"
            return web.json_response(upstream, status=status)

        if upstream.get("status") == "completed":
            merged = await _finalize_structured(run_id, upstream, headers)
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

        upstream_events_ok = False
        try:
            timeout = ClientTimeout(total=None, sock_read=None)
            async with ClientSession(timeout=timeout) as session:
                async with session.get(f"{API_BASE}/v1/runs/{run_id}/events", headers=headers) as upstream_resp:
                    if upstream_resp.status >= 400:
                        text = await upstream_resp.text()
                        # Important: Hermes can return run_not_found for the
                        # event stream while /v1/runs/{id} still exists and is
                        # running. Do not surface this as a terminal failure;
                        # fall back to polling below.
                        await response.write(
                            f"event: proxy.fallback\ndata: {json.dumps({'reason': 'upstream_events_unavailable', 'status': upstream_resp.status, 'upstream_error': text})}\n\n".encode()
                        )
                    else:
                        upstream_events_ok = True
                        async for chunk in upstream_resp.content.iter_chunked(4096):
                            if chunk:
                                await response.write(chunk)
        except (ConnectionResetError, asyncio.CancelledError):
            return response
        except Exception as exc:
            await response.write(f"event: proxy.fallback\ndata: {json.dumps({'reason': 'upstream_events_exception', 'error': str(exc)})}\n\n".encode())

        # After upstream stream ends (or if upstream events are unavailable),
        # poll status until terminal. This prevents false structured.failed
        # events for long-running runs whose SSE buffer is unavailable.
        while True:
            status, upstream = await _json_request("GET", f"/v1/runs/{run_id}", headers=_headers_from_request(request))
            if status >= 400:
                if status in {401, 403}:
                    await response.write(
                        f"event: structured.failed\ndata: {json.dumps({'run_id': run_id, 'structured_error': upstream})}\n\n".encode()
                    )
                    return response
                with _STATE_LOCK:
                    meta = copy.deepcopy(_runs.get(run_id))
                if meta and meta.get("upstream_snapshot"):
                    upstream = copy.deepcopy(meta["upstream_snapshot"])
                    upstream["upstream_status_unavailable"] = True
                else:
                    recovered = _session_recovery_snapshot(run_id)
                    if recovered:
                        upstream = recovered
                    else:
                        await response.write(
                            f"event: status\ndata: {json.dumps({'run_id': run_id, 'status': 'unknown', 'upstream_error': upstream})}\n\n".encode()
                        )
                        await asyncio.sleep(3)
                        continue

            upstream_status = upstream.get("status")
            if upstream_status not in _TERMINAL:
                await response.write(
                    f"event: status\ndata: {json.dumps({'run_id': run_id, 'status': upstream_status, 'source': 'upstream_sse' if upstream_events_ok else 'poll_fallback'})}\n\n".encode()
                )
                await asyncio.sleep(3)
                continue

            if upstream_status == "completed":
                merged = await _finalize_structured(run_id, upstream, headers)
            else:
                with _STATE_LOCK:
                    meta = _runs.get(run_id)
                    if meta and not meta.get("structured_done"):
                        meta["structured_done"] = True
                        meta["structured_status"] = "skipped"
                        meta["structured_error"] = f"upstream_{upstream_status}"
                        meta["upstream_snapshot"] = copy.deepcopy(upstream)
                        _save_state()
                    merged = _merge_structured(upstream, meta)

            sstatus = merged.get("structured_status")
            if sstatus == "completed":
                event_name = "structured.completed"
            elif sstatus == "skipped":
                event_name = "structured.skipped"
            else:
                event_name = "structured.failed"
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
