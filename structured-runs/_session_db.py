"""Read-only access to the Hermes ``state.db`` and the session-settle wait.

``/v1/runs`` status/events are an in-memory registry that can disappear while the
persistent API session still exists. These helpers let the wrapper recover a
run snapshot, wait for background delegations to deliver, and read the latest
persisted assistant reply -- all without ever writing to the db Hermes core owns.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from typing import Any, Dict, Optional

from . import _config as cfg
from ._config import _now

logger = logging.getLogger("structured-runs")


def _connect_state_db() -> sqlite3.Connection:
    """Open the Hermes state db read side with a bounded busy timeout.

    Hermes core writes to the same file. ``timeout`` is sqlite's busy timeout, so
    brief write locks are waited out instead of raising immediately; the PRAGMA
    keeps that guarantee even if a connection default is changed elsewhere.
    """
    con = sqlite3.connect(str(cfg.STATE_DB), timeout=cfg.STATE_DB_BUSY_TIMEOUT_S)
    con.execute(f"PRAGMA busy_timeout={int(cfg.STATE_DB_BUSY_TIMEOUT_S * 1000)}")
    return con


@contextlib.contextmanager
def _state_db(*, row_factory: bool = False):
    """Yield a read connection to the Hermes state db, always closing it.

    Yields None when the db file is absent so callers can degrade cleanly
    without duplicating the existence check and the close boilerplate.
    """
    if not cfg.STATE_DB.exists():
        yield None
        return
    con = _connect_state_db()
    if row_factory:
        con.row_factory = sqlite3.Row
    try:
        yield con
    finally:
        con.close()


def session_recovery_snapshot(run_id: str) -> Optional[Dict[str, Any]]:
    """Recover an upstream-like run snapshot from the Hermes session db."""
    try:
        with _state_db(row_factory=True) as con:
            if con is None:
                return None
            return _recovery_snapshot_from_db(con, run_id)
    except Exception:
        logger.exception("[structured-runs] Failed to recover session snapshot for %s", run_id)
        return None


def _recovery_snapshot_from_db(con: sqlite3.Connection, run_id: str) -> Optional[Dict[str, Any]]:
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


def session_work_state(run_id: str) -> Dict[str, Any]:
    """Return whether delegated work or delivery is still pending for a session.

    ``available`` is False when the durable delegation state could not be read.
    ``reason`` then distinguishes a deployment with no state db (``no_state_db``,
    a stable condition) from a failed query against an existing db
    (``query_failed``, e.g. a locked db or Hermes-core schema drift), which
    callers must treat as "unknown", not "nothing pending".
    """
    if not cfg.STATE_DB.exists():
        return {
            "available": False,
            "reason": "no_state_db",
            "pending_delegations": 0,
            "pending_delivery": 0,
            "last_activity_at": None,
        }
    try:
        with _state_db() as con:
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
    except Exception:
        logger.warning(
            "[structured-runs] Could not inspect delegated work for %s "
            "(locked db or Hermes-core schema drift?)",
            run_id,
            exc_info=True,
        )
        return {
            "available": False,
            "reason": "query_failed",
            "pending_delegations": 0,
            "pending_delivery": 0,
            "last_activity_at": None,
        }


def latest_session_output(run_id: str) -> Optional[Dict[str, Any]]:
    """Return the newest non-tool assistant reply persisted for this session."""
    try:
        with _state_db(row_factory=True) as con:
            if con is None:
                return None
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
    except Exception:
        logger.debug("[structured-runs] Could not read latest output for %s", run_id, exc_info=True)
        return None


async def wait_for_session_settle(run_id: str) -> Dict[str, Any]:
    """Wait for delegated work delivery and a brief quiet window.

    ``/v1/runs`` can report completed while a background ``delegate_task`` is still
    delivering its result to the same session. Finalizing immediately would
    capture a stale assistant reply. We wait for the session's own durable
    delegation records, with a bounded timeout.

    When the delegation state cannot be read we do NOT assume "nothing pending":
    a ``query_failed`` db keeps us polling until the timeout, and only a
    deployment with no state db at all skips the wait entirely (``unavailable``).
    """
    deadline = _now() + cfg.SESSION_SETTLE_TIMEOUT_S
    last_state: Dict[str, Any] = {}
    while _now() < deadline:
        state = session_work_state(run_id)
        last_state = state
        if not state.get("available"):
            if state.get("reason") == "no_state_db":
                return {"status": "unavailable", **state}
            await asyncio.sleep(cfg.SESSION_SETTLE_POLL_INTERVAL_S)
            continue
        last_activity = state.get("last_activity_at")
        quiet = last_activity is None or _now() - float(last_activity) >= cfg.SESSION_QUIET_S
        if (
            state.get("pending_delegations", 0) == 0
            and state.get("pending_delivery", 0) == 0
            and quiet
        ):
            return {"status": "settled", **state}
        await asyncio.sleep(cfg.SESSION_SETTLE_POLL_INTERVAL_S)
    return {"status": "timeout", **last_state}
