"""Post-completion output check + schema finalizer.

After the upstream run reaches a terminal state the wrapper:
  1. waits for this session's background delegations to deliver;
  2. attempt 0: converts the agent's own final answer to schema-valid JSON via
     ``llm.complete_structured``;
  3. if that is not schema-valid (mode ``auto``) or always (mode ``always``),
     loops up to ``FINAL_CHECK_MAX_ATTEMPTS`` (hard cap 7) agent re-check turns
     in the same session (``post_completion_final_check``), feeding the last
     validation error back into each retry prompt;
  4. canonicalizes ``*_path`` values and enriches ``*_url`` fields.

Every attempt is recorded in ``final_output_check.history``. A failed / expired
re-check falls back to the original completed output -- it can never erase a
valid result. If a re-check ever produces valid JSON that result is committed
(``resolved_on_attempt``); otherwise ``status`` is ``exhausted`` / ``fallback``.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from . import _config as cfg
from . import _media as media
from . import _schema as schema_mod
from . import _session_db as session_db
from . import _state
from . import _upstream as upstream
from ._config import _now

logger = logging.getLogger("structured-runs")


def final_output_check_prompt(
    schema: Dict[str, Any],
    verified_artifacts: Optional[List[str]] = None,
    *,
    attempt: int = 1,
    prior_error: Optional[str] = None,
    prior_parsed_preview: Optional[str] = None,
) -> str:
    """Build a schema-aware, post-completion correction request for the agent.

    Deliberately sent as a follow-up API run in the same session after the
    original work finishes. It does not assume a particular schema shape or
    artifact type: every client-provided JSON Schema gets the same final-output
    check, while artifact/file fields receive explicit MEDIA path guidance.

    ``attempt`` / ``prior_error`` / ``prior_parsed_preview`` add retry context
    when an earlier finalize pass still failed validation.

    Note: this prompt is intentionally written in Vietnamese -- it serves the
    Vietnamese end users of this deployment. Keep it that way when editing.
    """
    schema_text = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    retry_prefix = ""
    if prior_error:
        retry_prefix = (
            f"ĐÂY LÀ LẦN SỬA THỨ {attempt}. Finalizer VẪN chưa tạo được JSON hợp lệ từ câu trả lời trước.\n"
            f"Lỗi validate gần nhất: {prior_error}\n"
        )
        if prior_parsed_preview:
            retry_prefix += f"JSON finalizer vừa tạo (CHƯA ĐẠT): {prior_parsed_preview}\n"
        retry_prefix += (
            "Tập trung sửa ĐÚNG field gây lỗi ở trên. ĐỪNG lặp lại y hệt câu trả lời cũ. "
            "Nếu một field bắt buộc thực sự không có dữ liệu, nói rõ TÊN FIELD và LÝ DO.\n\n---\n\n"
        )
    prompt = retry_prefix + (
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


def run_output_text(status: Dict[str, Any]) -> str:
    output = status.get("output")
    if output is None:
        output = status.get("final_output") or status.get("result")
    if output is None:
        output = json.dumps(status, ensure_ascii=False)
    return str(output)


def merge_structured(upstream_status: Dict[str, Any], meta: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    merged = dict(upstream_status)
    if not meta:
        return merged
    parsed = meta.get("parsed")
    if meta.get("run_id") and parsed is not None:
        parsed = media.enrich_media_urls(meta["run_id"], parsed)
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
            "structured_validation": meta.get("structured_validation"),
            "final_output_check": meta.get("final_output_check"),
            "session_settle": meta.get("session_settle"),
            "verified_artifacts": meta.get("verified_artifacts", []),
        }
    )
    return merged


async def post_completion_final_check(
    run_id: str,
    upstream_status: Dict[str, Any],
    schema: Dict[str, Any],
    headers: Dict[str, str],
    verified_artifacts: Optional[List[str]] = None,
    *,
    attempt: int = 1,
    prior_error: Optional[str] = None,
    prior_parsed_preview: Optional[str] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Ask the completed upstream agent to produce a corrected final answer.

    The follow-up uses the same Hermes session as the original ``/v1/runs`` call,
    making the check an actual agent turn after completion. A failed/expired
    follow-up falls back to the original output.
    """
    original_output = run_output_text(upstream_status)
    session_id = upstream_status.get("session_id") or run_id
    prompt = final_output_check_prompt(
        schema,
        verified_artifacts,
        attempt=attempt,
        prior_error=prior_error,
        prior_parsed_preview=prior_parsed_preview,
    )
    prompt += "\n\nCâu trả lời cuối trước khi kiểm tra:\n" + original_output[: cfg.MAX_OUTPUT_CHARS]
    followup_headers = dict(headers)
    followup_headers["Content-Type"] = "application/json"
    status, started = await upstream.json_request(
        "POST",
        "/v1/runs",
        headers=followup_headers,
        body={"input": prompt, "session_id": session_id},
        timeout_s=30,
    )
    if status >= 400 or not started.get("run_id"):
        return original_output, {
            "status": "fallback",
            "run_id": None,
            "error": f"final_output_check_start_failed: {started}",
        }

    check_run_id = str(started["run_id"])
    deadline = _now() + cfg.FINAL_CHECK_TIMEOUT_S
    while _now() < deadline:
        status, checked = await upstream.json_request(
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
            checked_output = run_output_text(checked)
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
        await asyncio.sleep(cfg.FINAL_CHECK_POLL_INTERVAL_S)

    return original_output, {
        "status": "fallback",
        "run_id": check_run_id,
        "error": "final_output_check_timeout",
    }


_FINALIZER_INSTRUCTIONS = (
    "Bạn là bước finalizer của Hermes /v1/runs. "
    "Input là output cuối cùng của agent sau khi agent đã dùng tool nếu cần. "
    "Chuyển output đó thành JSON đúng schema. "
    "Không bịa dữ liệu ngoài output agent. Nếu thông tin không có, dùng giá trị rỗng/phù hợp schema. "
    "Tuyệt đối không thêm field ngoài schema."
)


async def _extract_json(
    llm: Any, run_id: str, output_text: str, schema: Dict[str, Any], schema_name: str
) -> Dict[str, Any]:
    """Run the finalizer LLM once on ``output_text``.

    Pure: canonicalizes / enriches / validates but does not touch the registry.
    Returns ``{parsed, validation_error, result, exc}`` (any of which may be None).
    """
    try:
        result = await asyncio.to_thread(
            llm.complete_structured,
            instructions=_FINALIZER_INSTRUCTIONS,
            input=[{"type": "text", "text": output_text}],
            json_schema=schema,
            schema_name=schema_name,
            purpose="structured-runs.finalizer",
            temperature=0.0,
        )
    except Exception as exc:  # noqa: BLE001 - reported to the caller
        logger.exception("[structured-runs] Finalizer exception for %s", run_id)
        return {"parsed": None, "validation_error": None, "result": None, "exc": exc}

    parsed = media.canonicalize_artifact_paths(result.parsed)
    parsed = media.enrich_media_urls(run_id, parsed)
    validation_error = schema_mod.validate_parsed(parsed, schema)
    return {"parsed": parsed, "validation_error": validation_error, "result": result, "exc": None}


async def finalize_structured(
    llm: Any, run_id: str, upstream_status: Dict[str, Any], headers: Dict[str, str]
) -> Dict[str, Any]:
    """Idempotent, concurrency-safe finalization for one completed run."""
    with _state.LOCK:
        meta = _state.runs.get(run_id)
        if not meta:
            return upstream_status
        if meta.get("structured_done"):
            return merge_structured(upstream_status, meta)
        if upstream_status.get("status") != "completed":
            return merge_structured(upstream_status, meta)
        if meta.get("structured_status") == "running":
            return merge_structured(upstream_status, meta)
        meta["structured_status"] = "running"
        meta["structured_started_at"] = _now()
        _state.save_state()

    # A terminal /v1/runs state can precede delivery of background delegation
    # results. Wait for durable delegation delivery, then read the latest
    # persisted assistant reply rather than freezing the first terminal response.
    settle = await session_db.wait_for_session_settle(run_id)
    latest_output = session_db.latest_session_output(run_id)
    if latest_output and latest_output.get("output"):
        upstream_status = dict(upstream_status)
        upstream_status["output"] = latest_output["output"]
        upstream_status["last_event"] = "session.settled"
        upstream_status["session_final_message_id"] = latest_output["message_id"]

    # Persist the settled terminal snapshot before finalizing. Hermes keeps run
    # statuses only briefly; after gateway restart or retention expiry
    # /v1/runs/{id} may return 404 while this wrapper still has the schema and
    # final structured result.
    with _state.LOCK:
        if run_id in _state.runs:
            _state.runs[run_id]["session_settle"] = settle
            _state.runs[run_id]["upstream_snapshot"] = copy.deepcopy(upstream_status)
            _state.save_state()

    schema = meta["json_schema"]
    schema_name = meta.get("schema_name") or "run.finalizer"
    original_output = run_output_text(upstream_status)
    verified_artifacts = media.verified_artifacts_from_text(original_output)
    artifact_suffix = ""
    if verified_artifacts:
        artifact_suffix = "\n\nARTIFACT ĐÃ XÁC MINH BỞI WRAPPER:\n" + "\n".join(
            f"- {path}" for path in verified_artifacts
        )

    def _clip(text: str) -> str:
        return text[: cfg.MAX_OUTPUT_CHARS]

    def _preview(text: Optional[str]) -> Optional[str]:
        n = cfg.FINAL_CHECK_TEXT_PREVIEW_CHARS
        return text[:n] if text and n > 0 else None

    def _ok(ex: Dict[str, Any]) -> bool:
        return ex["exc"] is None and (
            not schema_mod.validation_available() or ex["validation_error"] is None
        )

    def _history_entry(attempt: int, kind: str, ex: Dict[str, Any], rc: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "attempt": attempt,
            "kind": kind,
            "recheck_run_id": rc.get("run_id") if rc else None,
            "recheck_status": rc.get("status") if rc else None,
            "recheck_error": rc.get("error") if rc else None,
            "outcome": "valid" if _ok(ex) else ("finalizer_error" if ex["exc"] else "invalid"),
            "validation_error": str(ex["exc"]) if ex["exc"] else ex["validation_error"],
            "finalizer_text_preview": _preview(getattr(ex["result"], "text", None)) if ex["result"] else None,
        }

    mode = cfg.FINAL_CHECK_MODE  # "auto" | "always" | "off"
    max_attempts = cfg.FINAL_CHECK_MAX_ATTEMPTS
    history: List[Dict[str, Any]] = []
    attempts_run = 0
    consecutive_fallback = 0
    last_recheck_run_id: Optional[str] = None

    def _preview_parsed(ex: Dict[str, Any]) -> Optional[str]:
        if ex["parsed"] is None:
            return None
        return _preview(json.dumps(ex["parsed"], ensure_ascii=False))

    def _err_text(ex: Dict[str, Any]) -> Optional[str]:
        return str(ex["exc"]) if ex["exc"] else ex["validation_error"]

    # Attempt 0: finalize the agent's own output directly (no agent turn).
    extract = await _extract_json(
        llm, run_id, _clip(original_output + artifact_suffix), schema, schema_name
    )
    history.append(_history_entry(0, "agent_output", extract, None))
    committed = extract if _ok(extract) else None
    resolved_on_attempt = 0 if committed is not None else None
    stopped_on_fallback = False

    if mode == "off":
        fc_status, fc_reason = "skipped", "final_check_disabled"
    elif mode == "auto" and committed is not None:
        fc_status, fc_reason = "skipped", "first_pass_schema_valid"
    else:
        fc_reason = None
        prior_error = _err_text(extract)
        prior_preview = _preview_parsed(extract)
        # 'always' runs at least one re-check even if attempt 0 was valid.
        need_more = mode == "always" or committed is None
        while need_more and attempts_run < max_attempts:
            n = attempts_run + 1
            logger.info(
                "[structured-runs] Post-completion output check for %s attempt %d/%d",
                run_id, n, max_attempts,
            )
            checked_output, rc = await post_completion_final_check(
                run_id, upstream_status, schema, headers, verified_artifacts,
                attempt=n, prior_error=prior_error, prior_parsed_preview=prior_preview,
            )
            attempts_run = n
            last_recheck_run_id = rc.get("run_id") or last_recheck_run_id
            extract = await _extract_json(
                llm, run_id, _clip(checked_output + artifact_suffix), schema, schema_name
            )
            history.append(_history_entry(n, "agent_recheck", extract, rc))
            if _ok(extract):
                committed = extract
                resolved_on_attempt = n
                break
            prior_error = _err_text(extract)
            prior_preview = _preview_parsed(extract)
            if rc.get("status") == "fallback":
                consecutive_fallback += 1
                if (
                    cfg.FINAL_CHECK_STOP_ON_FALLBACK
                    and consecutive_fallback >= cfg.FINAL_CHECK_STOP_ON_FALLBACK
                ):
                    stopped_on_fallback = True
                    break
            else:
                consecutive_fallback = 0
            need_more = committed is None

        if committed is not None:
            fc_status = "completed"
        elif stopped_on_fallback:
            fc_status = "fallback"
        else:
            fc_status = "exhausted"

    # Commit the valid attempt if we found one, else the last attempt tried.
    if committed is not None:
        extract = committed
    final_check = {
        "status": fc_status,
        "reason": fc_reason,
        "recheck_attempts": attempts_run,
        "resolved_on_attempt": resolved_on_attempt,
        "last_recheck_run_id": last_recheck_run_id,
        "run_id": last_recheck_run_id,  # back-compat alias
        "history": history,
    }

    with _state.LOCK:
        if run_id in _state.runs:
            _state.runs[run_id]["final_output_check"] = final_check
            _state.runs[run_id]["verified_artifacts"] = verified_artifacts
            _state.save_state()
    logger.info("[structured-runs] Finalizing %s using schema=%s", run_id, schema_name)

    validation_mode = "enforced" if schema_mod.validation_available() else "skipped_no_jsonschema"
    if not schema_mod.validation_available():
        logger.warning(
            "[structured-runs] jsonschema not installed: schema (incl. "
            "additionalProperties) NOT enforced for %s",
            run_id,
        )

    parsed = extract.get("parsed")
    validation_error = extract.get("validation_error")
    result = extract.get("result")
    exc = extract.get("exc")
    with _state.LOCK:
        meta = _state.runs.get(run_id)
        if meta:
            meta["structured_done"] = True
            meta["structured_finished_at"] = _now()
            meta["structured_validation"] = validation_mode
            if result is not None:
                meta["content_type"] = result.content_type
                meta["structured_model"] = result.model
                meta["structured_usage"] = {
                    "input_tokens": result.usage.input_tokens,
                    "output_tokens": result.usage.output_tokens,
                    "total_tokens": result.usage.total_tokens,
                }
            if exc is not None:
                meta["structured_status"] = "failed"
                meta["structured_error"] = str(exc)
            elif validation_error:
                meta["structured_status"] = "failed"
                meta["structured_error"] = validation_error
                meta["parsed"] = None
                if result is not None:
                    meta["raw_structured_text"] = result.text[:1000]
            else:
                meta["structured_status"] = "completed"
                meta["structured_error"] = None
                meta["parsed"] = parsed
            _state.save_state()

    with _state.LOCK:
        return merge_structured(upstream_status, _state.runs.get(run_id))
