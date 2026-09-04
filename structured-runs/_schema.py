"""JSON Schema validation for the finalizer contract.

``jsonschema`` is an optional runtime dependency: when it is missing the wrapper
still runs but cannot enforce the client contract (including
``additionalProperties: false``). Callers surface that via ``structured_validation``.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

try:
    import jsonschema  # type: ignore
except Exception:  # pragma: no cover - runtime optional
    jsonschema = None  # type: ignore


def validation_available() -> bool:
    return jsonschema is not None


def verbatim_field_name(schema: Dict[str, Any]) -> Optional[str]:
    """Return the one top-level property marked ``x-verbatim-from-output``, if any.

    ``x-verbatim-from-output: true`` on a top-level string property tells the
    finalizer to skip asking the LLM to reproduce that field and substitute
    the run's raw ``output`` directly instead (see _finalize.py). At most one
    property may carry the marker -- there is only one ``output`` blob to
    substitute, so a second marked field would silently receive the exact
    same text, which is never what a schema author actually wants. Callers
    that need more than one large verbatim block should split the work into
    separate structured runs instead.
    """
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return None
    marked = [
        name
        for name, prop in properties.items()
        if isinstance(prop, dict) and prop.get("x-verbatim-from-output") is True
    ]
    return marked[0] if len(marked) == 1 else None


def schema_error(schema: Any) -> Optional[str]:
    """Return why ``schema`` is unusable as a final-output contract, or None."""
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
    properties = schema.get("properties")
    if isinstance(properties, dict):
        marked = [
            name
            for name, prop in properties.items()
            if isinstance(prop, dict) and prop.get("x-verbatim-from-output") is True
        ]
        if len(marked) > 1:
            return (
                "at most one property may set x-verbatim-from-output: true "
                f"(got {sorted(marked)}); split into separate structured runs "
                "instead of marking more than one field"
            )
        for name in marked:
            prop_type = properties[name].get("type")
            if prop_type != "string":
                return f"x-verbatim-from-output on '{name}' requires type: 'string' (got {prop_type!r})"
    return None


def validate_parsed(parsed: Any, schema: Dict[str, Any]) -> Optional[str]:
    """Return a validation error string, or None when parsed satisfies schema."""
    if parsed is None:
        return "finalizer_returned_non_json"
    if jsonschema is None:
        # Cannot validate without the dependency. The run is not failed for this
        # alone, but the caller is told via `structured_validation` and a warning
        # is logged at finalize time.
        return None
    try:
        jsonschema.validate(instance=parsed, schema=schema)
        return None
    except Exception as exc:
        return f"schema_validation_failed: {exc}"
