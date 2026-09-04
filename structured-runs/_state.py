"""In-memory run registry and its JSON persistence.

The registry (`runs`) maps upstream ``run_id`` -> wrapper metadata (schema,
structured result, terminal snapshot). It is persisted to
``~/.hermes/structured_runs_state.json`` so poll still works after a gateway
restart. Request ``Authorization`` headers are never persisted.
"""
from __future__ import annotations

import copy
import json
import logging
import threading
from typing import Any, Dict

from . import _config as cfg
from ._config import _now

logger = logging.getLogger("structured-runs")

LOCK = threading.RLock()
runs: Dict[str, Dict[str, Any]] = {}


def load_state() -> None:
    """Load the persisted registry, then repair interrupted finalizers."""
    global runs
    try:
        if cfg.STATE_FILE.exists():
            data = json.loads(cfg.STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                runs = data.get("runs", {}) if isinstance(data.get("runs"), dict) else {}
    except Exception:
        logger.exception("[structured-runs] Failed to load state file %s", cfg.STATE_FILE)
        runs = {}
    _recover_interrupted_finalizers()


def _recover_interrupted_finalizers() -> None:
    """Rewind finalizers that were mid-flight when the process last stopped.

    ``finalize_structured`` persists ``structured_status = "running"`` before its
    long settle / final-check / finalizer awaits, and treats an existing
    "running" as "another task already owns this run". If the gateway restarts
    inside that window the run would stay "running" forever and never re-finalize.
    On load, any run that is "running" without ``structured_done`` is orphaned:
    reset it to "pending" so the next poll re-runs the (idempotent) finalizer.
    """
    changed = False
    for meta in runs.values():
        if not isinstance(meta, dict):
            continue
        if meta.get("structured_status") == "running" and not meta.get("structured_done"):
            meta["structured_status"] = "pending"
            meta.pop("structured_started_at", None)
            changed = True
    if changed:
        logger.info("[structured-runs] Reset orphaned in-progress finalizer(s) on load")
        save_state()


def _run_is_evictable(meta: Dict[str, Any]) -> bool:
    """Only finished runs may be dropped; in-flight work keeps its tracking."""
    if meta.get("structured_done"):
        return True
    return meta.get("structured_status") in {"completed", "failed", "skipped"}


def _run_age_s(meta: Dict[str, Any]) -> float:
    ts = meta.get("structured_finished_at") or meta.get("created_at") or 0.0
    try:
        return max(0.0, _now() - float(ts))
    except (TypeError, ValueError):
        return 0.0


def evict_runs_locked() -> bool:
    """Bound the run registry. Caller must hold ``LOCK``.

    Drops finished runs older than ``STRUCTURED_RUNS_RETENTION_S``, then, if still
    above ``STRUCTURED_RUNS_MAX_TRACKED``, drops the oldest finished runs until the
    cap is met. In-flight runs are never dropped. Returns True if anything was removed.
    """
    removed = 0
    if cfg.RUN_RETENTION_S > 0:
        stale = [
            rid
            for rid, meta in runs.items()
            if isinstance(meta, dict)
            and _run_is_evictable(meta)
            and _run_age_s(meta) > cfg.RUN_RETENTION_S
        ]
        for rid in stale:
            runs.pop(rid, None)
            removed += 1
    if 0 < cfg.MAX_TRACKED_RUNS < len(runs):
        evictable = sorted(
            (rid for rid, meta in runs.items() if isinstance(meta, dict) and _run_is_evictable(meta)),
            key=lambda rid: _run_age_s(runs[rid]),
            reverse=True,
        )
        for rid in evictable[: len(runs) - cfg.MAX_TRACKED_RUNS]:
            runs.pop(rid, None)
            removed += 1
    if removed:
        logger.info("[structured-runs] Evicted %d finished run(s) from registry", removed)
    return bool(removed)


def save_state() -> None:
    try:
        cfg.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = cfg.STATE_FILE.with_suffix(".tmp")
        with LOCK:
            evict_runs_locked()
            # Do not persist request Authorization headers.
            serializable = {"runs": copy.deepcopy(runs), "updated_at": _now()}
            for meta in serializable["runs"].values():
                meta.pop("headers", None)
                meta.pop("finalize_task", None)
        tmp.write_text(json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(cfg.STATE_FILE)
    except Exception:
        logger.exception("[structured-runs] Failed to save state file %s", cfg.STATE_FILE)
