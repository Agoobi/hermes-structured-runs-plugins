"""Unit tests for the schema-aware post-completion output check."""
from __future__ import annotations

import importlib.util
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


if __name__ == "__main__":
    unittest.main()
