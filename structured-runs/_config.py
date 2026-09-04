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
# Max agent re-check turns when the finalizer output is still not schema-valid.
# Attempt 0 (finalizing the agent's own output, no agent turn) does not count.
# Hard-clamped to [0, 7]: each attempt is an agent turn + a complete_structured call.
_FINAL_CHECK_MAX_ATTEMPTS_CAP = 7
FINAL_CHECK_MAX_ATTEMPTS = max(
    0, min(int(os.getenv("STRUCTURED_RUNS_FINAL_CHECK_MAX_ATTEMPTS", "3")), _FINAL_CHECK_MAX_ATTEMPTS_CAP)
)
# Stop the re-check loop early after this many consecutive re-checks whose agent
# turn could not run (fallback). 0 disables the early stop.
FINAL_CHECK_STOP_ON_FALLBACK = max(0, int(os.getenv("STRUCTURED_RUNS_FINAL_CHECK_STOP_ON_FALLBACK", "2")))
# How much of each attempt's raw finalizer text to keep in final_output_check.history.
FINAL_CHECK_TEXT_PREVIEW_CHARS = max(0, int(os.getenv("STRUCTURED_RUNS_FINAL_CHECK_TEXT_PREVIEW_CHARS", "500")))
SESSION_SETTLE_TIMEOUT_S = float(os.getenv("STRUCTURED_RUNS_SESSION_SETTLE_TIMEOUT_S", "180"))
SESSION_QUIET_S = float(os.getenv("STRUCTURED_RUNS_SESSION_QUIET_S", "3"))
SESSION_SETTLE_POLL_INTERVAL_S = float(os.getenv("STRUCTURED_RUNS_SESSION_SETTLE_POLL_INTERVAL_S", "1"))
SSE_UNKNOWN_TIMEOUT_S = float(os.getenv("STRUCTURED_RUNS_SSE_UNKNOWN_TIMEOUT_S", "90"))
SSE_KEEPALIVE_S = float(os.getenv("STRUCTURED_RUNS_SSE_KEEPALIVE_S", "15"))
STATE_DB_BUSY_TIMEOUT_S = float(os.getenv("STRUCTURED_RUNS_STATE_DB_BUSY_TIMEOUT_S", "5"))
# Per-run SSE event buffer, so clients can replay after a reconnect (Hermes core
# neither buffers nor broadcasts run events).
EVENT_LOG_MAX_EVENTS = int(os.getenv("STRUCTURED_RUNS_EVENT_LOG_MAX_EVENTS", "3000"))
EVENT_LOG_TTL_S = float(os.getenv("STRUCTURED_RUNS_EVENT_LOG_TTL_S", "600"))
EVENT_LOG_MAX_RUNS = int(os.getenv("STRUCTURED_RUNS_EVENT_LOG_MAX_RUNS", "500"))
RUN_RETENTION_S = float(os.getenv("STRUCTURED_RUNS_RETENTION_S", str(7 * 24 * 3600)))
MAX_TRACKED_RUNS = int(os.getenv("STRUCTURED_RUNS_MAX_TRACKED", "2000"))

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
