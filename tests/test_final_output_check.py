"""Unit tests for the schema-aware post-completion output check."""
from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path


PLUGIN_PATH = Path(__file__).resolve().parents[1] / "structured-runs" / "__init__.py"
spec = importlib.util.spec_from_file_location("structured_runs_plugin", PLUGIN_PATH)
assert spec and spec.loader
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


class FinalOutputCheckPromptTests(unittest.TestCase):
    def test_includes_arbitrary_client_schema_and_media_contract(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "report_path": {"type": "string"},
                "summary": {"type": "string"},
            },
            "required": ["report_path", "summary"],
            "additionalProperties": False,
        }

        prompt = plugin._final_output_check_prompt(schema)

        self.assertIn('"report_path":{"type":"string"}', prompt)
        self.assertIn('"summary":{"type":"string"}', prompt)
        self.assertIn("MEDIA:/absolute/path/to/file.ext", prompt)
        self.assertIn("relative path", prompt)
        self.assertIn("Không thêm field ngoài schema", prompt)

    def test_extracts_output_from_all_supported_run_shapes(self) -> None:
        self.assertEqual(plugin._run_output_text({"output": "first"}), "first")
        self.assertEqual(plugin._run_output_text({"final_output": "second"}), "second")
        self.assertEqual(plugin._run_output_text({"result": "third"}), "third")

    def test_falls_back_to_json_for_missing_output(self) -> None:
        output = plugin._run_output_text({"status": "completed", "run_id": "run_123"})
        self.assertIn('"run_id": "run_123"', output)

    def test_wrapper_resolves_relative_artifact_without_agent_cwd(self) -> None:
        original_roots = plugin.MEDIA_ROOTS
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifact = root / "videos" / "demo" / "output" / "video.mp4"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"not-a-real-video")
            setattr(plugin, "MEDIA_ROOTS", [root])
            try:
                text = "File: videos/demo/output/video.mp4"
                self.assertEqual(plugin._verified_artifacts_from_text(text), [str(artifact)])
                self.assertEqual(
                    plugin._resolve_media_path("MEDIA:videos/demo/output/video.mp4"), artifact
                )
                parsed = plugin._canonicalize_artifact_paths(
                    {"media_path": "MEDIA:videos/demo/output/video.mp4"}
                )
                self.assertEqual(parsed["media_path"], str(artifact))
            finally:
                setattr(plugin, "MEDIA_ROOTS", original_roots)

    def test_session_state_tracks_pending_delegation_and_latest_reply(self) -> None:
        original_db = plugin.STATE_DB
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            con = sqlite3.connect(db_path)
            con.executescript(
                """
                create table sessions (id text primary key, last_activity_at real);
                create table async_delegations (
                    delegation_id text primary key,
                    origin_session text,
                    parent_session_id text,
                    origin_session_id text,
                    state text,
                    delivery_state text
                );
                create table messages (
                    id integer primary key,
                    session_id text,
                    role text,
                    active integer,
                    content text,
                    finish_reason text,
                    timestamp real
                );
                """
            )
            con.execute("insert into sessions values (?, ?)", ("run_test", 123.0))
            con.execute(
                "insert into async_delegations values (?, ?, ?, ?, ?, ?)",
                ("deleg_1", "run_test", "run_test", "run_test", "running", "pending"),
            )
            con.execute(
                "insert into messages values (?, ?, ?, ?, ?, ?, ?)",
                (1, "run_test", "assistant", 1, "old final", "stop", 1.0),
            )
            con.execute(
                "insert into messages values (?, ?, ?, ?, ?, ?, ?)",
                (2, "run_test", "assistant", 1, "new final", "stop", 2.0),
            )
            con.commit()
            con.close()
            setattr(plugin, "STATE_DB", db_path)
            try:
                state = plugin._session_work_state("run_test")
                self.assertEqual(state["pending_delegations"], 1)
                self.assertEqual(state["pending_delivery"], 0)
                self.assertEqual(plugin._latest_session_output("run_test")["output"], "new final")
            finally:
                setattr(plugin, "STATE_DB", original_db)


if __name__ == "__main__":
    unittest.main()
