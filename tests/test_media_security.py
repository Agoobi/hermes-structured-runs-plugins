"""Phase 2: media path resolution hardening — traversal and sensitive files."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from _plugin import _config, _media, _schema


class ResolveMediaPathSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_roots = _config.MEDIA_ROOTS
        self._orig_state_file = _config.STATE_FILE
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "root"
        (self.root / "sub").mkdir(parents=True)
        self.artifact = self.root / "sub" / "video.mp4"
        self.artifact.write_bytes(b"x")
        self.secret = Path(self._tmp.name) / "secret.txt"
        self.secret.write_bytes(b"top secret")
        _config.MEDIA_ROOTS = [self.root.resolve()]

    def tearDown(self) -> None:
        _config.MEDIA_ROOTS = self._orig_roots
        _config.STATE_FILE = self._orig_state_file
        self._tmp.cleanup()

    def test_resolves_legitimate_relative_artifact(self) -> None:
        self.assertEqual(_media.resolve_media_path("sub/video.mp4"), self.artifact.resolve())

    def test_rejects_relative_dotdot_traversal(self) -> None:
        self.assertIsNone(_media.resolve_media_path("sub/../../secret.txt"))
        self.assertIsNone(_media.resolve_media_path("../secret.txt"))

    def test_rejects_absolute_path_outside_roots(self) -> None:
        self.assertIsNone(_media.resolve_media_path(str(self.secret)))

    def test_rejects_media_marker_traversal(self) -> None:
        self.assertIsNone(_media.resolve_media_path("MEDIA:sub/../../secret.txt"))

    def test_rejects_null_byte(self) -> None:
        self.assertIsNone(_media.resolve_media_path("sub/video.mp4\x00.png"))

    def test_rejects_symlink_escaping_root(self) -> None:
        link = self.root / "escape.txt"
        try:
            link.symlink_to(self.secret)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not supported here")
        self.assertIsNone(_media.resolve_media_path("escape.txt"))

    def test_rejects_sqlite_db_even_under_root(self) -> None:
        for name in ("state.db", "state.db-wal", "state.db-shm", "notes.sqlite3"):
            f = self.root / name
            f.write_bytes(b"x")
            self.assertIsNone(_media.resolve_media_path(name), name)

    def test_rejects_wrapper_state_file_even_under_root(self) -> None:
        state = self.root / "structured_runs_state.json"
        state.write_bytes(b"{}")
        _config.STATE_FILE = state
        self.assertIsNone(_media.resolve_media_path("structured_runs_state.json"))


class ValidationMarkerTests(unittest.TestCase):
    def test_validate_parsed_rejects_bad_instance(self) -> None:
        schema = {
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "required": ["a"],
            "additionalProperties": False,
        }
        self.assertIsNone(_schema.validate_parsed({"a": "ok"}, schema))
        self.assertIsNotNone(_schema.validate_parsed({"a": 1}, schema))
        self.assertEqual(_schema.validate_parsed(None, schema), "finalizer_returned_non_json")

    def test_validation_available_reflects_jsonschema_import(self) -> None:
        self.assertEqual(_schema.validation_available(), _schema.jsonschema is not None)


if __name__ == "__main__":
    unittest.main()
