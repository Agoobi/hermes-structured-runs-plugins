"""Environment-derived configuration for the structured-runs wrapper.

Everything tunable lives here so the rest of the plugin reads one source of
truth. Other modules import this as ``from . import _config as cfg`` and read
``cfg.NAME`` at call time, which also keeps the values patchable in tests.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

API_BASE = os.getenv("STRUCTURED_RUNS_UPSTREAM", "http://127.0.0.1:8642").rstrip("/")
HERMES_HOME = Path(os.getenv("HERMES_HOME") or os.path.expanduser("~/.hermes"))
STATE_FILE = HERMES_HOME / "structured_runs_state.json"
STATE_DB = HERMES_HOME / "state.db"

MAX_OUTPUT_CHARS = int(os.getenv("STRUCTURED_RUNS_MAX_OUTPUT_CHARS", "200000"))
# How the post-completion agent re-check ("BƯỚC KIỂM TRA OUTPUT CUỐI") is used:
#   auto   - finalize the agent's own output first; only run the re-check when
#            that first pass is not schema-valid  (default)
#   always - always run the re-check before finalizing (legacy behavior)
#   off    - never run the re-check; the first-pass finalizer result is final
FINAL_CHECK_MODE = os.getenv("STRUCTURED_RUNS_FINAL_CHECK_MODE", "auto").strip().lower()
if FINAL_CHECK_MODE not in {"auto", "always", "off"}:
    FINAL_CHECK_MODE = "auto"
FINAL_CHECK_TIMEOUT_S = float(os.getenv("STRUCTURED_RUNS_FINAL_CHECK_TIMEOUT_S", "120"))
FINAL_CHECK_POLL_INTERVAL_S = float(os.getenv("STRUCTURED_RUNS_FINAL_CHECK_POLL_INTERVAL_S", "1"))
SESSION_SETTLE_TIMEOUT_S = float(os.getenv("STRUCTURED_RUNS_SESSION_SETTLE_TIMEOUT_S", "180"))
SESSION_QUIET_S = float(os.getenv("STRUCTURED_RUNS_SESSION_QUIET_S", "3"))
SESSION_SETTLE_POLL_INTERVAL_S = float(os.getenv("STRUCTURED_RUNS_SESSION_SETTLE_POLL_INTERVAL_S", "1"))
SSE_UNKNOWN_TIMEOUT_S = float(os.getenv("STRUCTURED_RUNS_SSE_UNKNOWN_TIMEOUT_S", "90"))
STATE_DB_BUSY_TIMEOUT_S = float(os.getenv("STRUCTURED_RUNS_STATE_DB_BUSY_TIMEOUT_S", "5"))
RUN_RETENTION_S = float(os.getenv("STRUCTURED_RUNS_RETENTION_S", str(7 * 24 * 3600)))
MAX_TRACKED_RUNS = int(os.getenv("STRUCTURED_RUNS_MAX_TRACKED", "2000"))

# Hard cap on a single finalization attempt. The finalizer must always leave a
# terminal structured state, so this also covers the two LLM calls, which have
# no timeout of their own, on top of the bounded settle / final-check waits.
FINALIZER_MAX_RUNTIME_S = float(
    os.getenv("STRUCTURED_RUNS_FINALIZER_MAX_RUNTIME_S")
    or (SESSION_SETTLE_TIMEOUT_S + FINAL_CHECK_TIMEOUT_S + 300.0)
)
# A "running" claim older than this belongs to a finalizer that died without
# unwinding (killed process), so the next poll may reclaim it.
FINALIZER_STALE_AFTER_S = float(
    os.getenv("STRUCTURED_RUNS_FINALIZER_STALE_AFTER_S") or (FINALIZER_MAX_RUNTIME_S * 2)
)
# Fail the run instead of re-claiming a finalizer that keeps dying.
FINALIZER_MAX_ATTEMPTS = int(os.getenv("STRUCTURED_RUNS_FINALIZER_MAX_ATTEMPTS", "3"))
# How long GET /v1/runs/structured/{id} blocks on an in-flight finalizer before
# answering "structured_status: running". Finalization continues either way, so
# this only bounds the poll, never the work.
POLL_FINALIZE_WAIT_S = float(os.getenv("STRUCTURED_RUNS_POLL_FINALIZE_WAIT_S", "20"))

MEDIA_PATH_RE = re.compile(
    r"(?:MEDIA:)?(?:(?:/|~?/)?(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.(?:mp4|mov|mkv|webm|mp3|wav|m4a|ogg|png|jpe?g|webp|gif|pdf|docx|xlsx|csv|zip))",
    re.IGNORECASE,
)
SENSITIVE_MEDIA_RE = re.compile(r"\.(db|sqlite|sqlite3)(-wal|-shm|-journal)?$", re.IGNORECASE)

MEDIA_ROOTS = [
    Path(p).expanduser().resolve()
    for p in os.getenv(
        "STRUCTURED_RUNS_MEDIA_ROOTS",
        "/root/motion-graphic-templete,/root/.hermes,/tmp",
    ).split(",")
    if p.strip()
]

TERMINAL_STATES = {"completed", "failed", "cancelled"}
HEADER_ALLOWLIST = {
    "authorization",
    "x-hermes-session-id",
    "x-hermes-session-key",
    "idempotency-key",
    "accept",
    "user-agent",
}


def _now() -> float:
    return time.time()
