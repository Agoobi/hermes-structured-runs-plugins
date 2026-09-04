"""Thin HTTP client for the real Hermes API server.

Only headers on the allowlist are forwarded upstream; nothing else about the
inbound request crosses the boundary.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple

from aiohttp import ClientSession, ClientTimeout, web

from . import _config as cfg

# One pooled session per plugin event loop for the short JSON calls. Created
# lazily inside the loop; never closed on purpose (the plugin runs for the life
# of the process). The long-lived SSE proxy stream keeps its own session.
_session: Optional[ClientSession] = None


def _get_session() -> ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = ClientSession()
    return _session


def headers_from_request(request: web.Request, *, json_body: bool = False) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    for key, value in request.headers.items():
        if key.lower() in cfg.HEADER_ALLOWLIST:
            headers[key] = value
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


async def json_request(
    method: str,
    path: str,
    *,
    headers: Dict[str, str],
    body: Optional[dict] = None,
    timeout_s: float = 600.0,
) -> Tuple[int, Dict[str, Any]]:
    session = _get_session()
    async with session.request(
        method,
        f"{cfg.API_BASE}{path}",
        headers=headers,
        json=body,
        timeout=ClientTimeout(total=timeout_s),
    ) as resp:
        text = await resp.text()
        try:
            data = json.loads(text) if text else {}
        except Exception:
            data = {"raw": text}
        return resp.status, data
