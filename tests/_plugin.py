"""Shared loader for the (hyphen-named, non-importable-by-name) plugin package.

Mirrors how Hermes core loads a directory plugin: ``spec_from_file_location``
with ``submodule_search_locations`` set and the module registered in
``sys.modules`` so the package's own ``from . import _foo`` relative imports
resolve. Tests then reach submodules as ``plugin._config`` etc.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parents[1] / "structured-runs"
_MODNAME = "structured_runs"

if _MODNAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        _MODNAME,
        _PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(_PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    module.__package__ = _MODNAME
    module.__path__ = [str(_PLUGIN_DIR)]
    sys.modules[_MODNAME] = module
    spec.loader.exec_module(module)

plugin = sys.modules[_MODNAME]

_config = importlib.import_module(f"{_MODNAME}._config")
_state = importlib.import_module(f"{_MODNAME}._state")
_session_db = importlib.import_module(f"{_MODNAME}._session_db")
_schema = importlib.import_module(f"{_MODNAME}._schema")
_media = importlib.import_module(f"{_MODNAME}._media")
_upstream = importlib.import_module(f"{_MODNAME}._upstream")
_events = importlib.import_module(f"{_MODNAME}._events")
_finalize = importlib.import_module(f"{_MODNAME}._finalize")
_app = importlib.import_module(f"{_MODNAME}._app")
