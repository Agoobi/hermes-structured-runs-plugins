"""
Plugin: Structured Runs Wrapper (production-safe)

Goal: keep Hermes /v1/runs behavior intact (real agent, real tools, real
polling/SSE/stop/approval), and add only a schema-validated finalizer.

Endpoints on :8646:
- POST /v1/runs/structured
- GET  /v1/runs/structured/{run_id}
- GET  /v1/runs/structured/{run_id}/events
- GET  /v1/runs/structured/{run_id}/media
- POST /v1/runs/structured/{run_id}/stop
- POST /v1/runs/structured/{run_id}/approval

The wrapper forwards to the real API server at STRUCTURED_RUNS_UPSTREAM
(default http://127.0.0.1:8642). Clients should send the same Bearer key as
for :8642. The wrapper persists schema/result metadata so poll still works
after gateway restart.

Module layout:
- _config      env-derived settings and shared constants
- _state       in-memory run registry + JSON persistence (load/save/recover/evict)
- _session_db  read-only Hermes state.db access + session-settle wait
- _schema      optional jsonschema validation of the finalizer contract
- _media       artifact path resolution + media-URL enrichment
- _upstream    allowlisted HTTP client for the real API server
- _finalize    post-completion agent check + complete_structured finalizer
- _app         the aiohttp routes
"""
from __future__ import annotations

import asyncio
import logging
import threading

from aiohttp import web

from . import _app, _config, _state

logger = logging.getLogger("structured-runs")


def register(ctx):
    _state.load_state()
    app = _app.build_app(ctx)

    def _run_server():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        runner = web.AppRunner(app)
        loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, "0.0.0.0", 8646)
        loop.run_until_complete(site.start())
        logger.info(
            "[structured-runs] listening on :8646 upstream=%s state=%s",
            _config.API_BASE,
            _config.STATE_FILE,
        )
        loop.run_forever()

    threading.Thread(target=_run_server, daemon=True).start()
    logger.info("[structured-runs] Plugin registered")
