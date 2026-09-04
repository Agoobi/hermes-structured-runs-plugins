"""Artifact path resolution and media-URL enrichment.

The wrapper only ever hands callers canonical filesystem paths under an allowed
root. ``MEDIA:`` is an input transport marker, not a stored value. Traversal,
symlink escape, sqlite databases and the wrapper's own state file are rejected.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional
from urllib.parse import quote

from . import _config as cfg


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except Exception:
        return False


def _is_sensitive_media(candidate: Path) -> bool:
    """Reject paths that are never legitimate run artifacts, even under a root.

    ``STRUCTURED_RUNS_MEDIA_ROOTS`` defaults to include ``~/.hermes``, which also
    holds this wrapper's state file and the Hermes ``state.db``. A finalizer that
    emits one of those as a ``*_path`` value must not turn the media route into a
    reader for schemas, cached results, or the session database.
    """
    if cfg.SENSITIVE_MEDIA_RE.search(candidate.name):
        return True
    try:
        state_files = {cfg.STATE_FILE.resolve(), cfg.STATE_FILE.with_suffix(".tmp").resolve()}
    except Exception:
        return True
    return candidate in state_files


def resolve_media_path(raw_path: str) -> Optional[Path]:
    """Resolve a run-produced artifact path without allowing traversal.

    Relative paths are resolved against the explicit allowed roots, not the
    gateway process cwd.
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
        for root in cfg.MEDIA_ROOTS:
            candidates.append((root / p).resolve())

    for candidate in candidates:
        if not candidate.is_file():
            continue
        if _is_sensitive_media(candidate):
            continue
        if any(_is_under(candidate, root) for root in cfg.MEDIA_ROOTS):
            return candidate
    return None


def verified_artifacts_from_text(text: str) -> List[str]:
    """Find existing artifacts mentioned in agent output and canonicalize them.

    Evidence-based: a value is returned only if it both appears in the agent
    output and resolves to an existing regular file under an allowed root. This
    makes a relative path independent of an agent's later working directory.
    """
    found: List[str] = []
    for match in cfg.MEDIA_PATH_RE.finditer(text or ""):
        resolved = resolve_media_path(match.group(0))
        if resolved:
            value = str(resolved)
            if value not in found:
                found.append(value)
    return found


def canonicalize_artifact_paths(parsed: Any) -> Any:
    """Normalize only existing ``*_path`` values to safe absolute paths."""
    if not isinstance(parsed, dict):
        return parsed
    out = dict(parsed)
    for key, value in out.items():
        if key.endswith("_path") and isinstance(value, str):
            resolved = resolve_media_path(value)
            if resolved:
                out[key] = str(resolved)
    return out


def media_url_for(run_id: str, media_path: str) -> str:
    return f"/v1/runs/structured/{quote(run_id, safe='')}/media?path={quote(media_path, safe='')}"


def enrich_media_urls(run_id: str, parsed: Any) -> Any:
    """Add URL fields when parsed JSON contains local media paths.

    Conservative: only mutate existing ``*_url`` keys that are missing, so
    ``additionalProperties: false`` stays safe (no new keys unless the
    schema/finalizer already included them).
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
            if resolve_media_path(path_val):
                out[url_key] = media_url_for(run_id, path_val)
    # Common case from schemas that use media_path + video_url.
    if isinstance(out.get("media_path"), str) and "video_url" in out and not out.get("video_url"):
        if resolve_media_path(out["media_path"]):
            out["video_url"] = media_url_for(run_id, out["media_path"])
    return out
