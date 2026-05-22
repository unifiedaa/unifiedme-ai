from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

tool_converter = importlib.import_module("unified.gumloop.tool_converter")
parse_tool_calls = tool_converter.parse_tool_calls
tools_to_system_prompt = tool_converter.tools_to_system_prompt


class GumloopToolConverterTests(unittest.TestCase):
    def test_tools_prompt_requests_json_tool_calls(self) -> None:
        prompt = tools_to_system_prompt(
            [
                {
                    "name": "read",
                    "description": "Read a file",
                    "input_schema": {
                        "type": "object",
                        "properties": {"filePath": {"type": "string"}},
                        "required": ["filePath"],
                    },
                }
            ]
        )

        self.assertIn('"tool_calls"', prompt)
        self.assertIn('"name": "read"', prompt)
        self.assertIn('"arguments": {', prompt)
        self.assertNotIn("<tool_use>", prompt)

    def test_parse_tool_calls_extracts_json_tool_calls(self) -> None:
        text = (
            'I will inspect the file.\n'
            '{"tool_calls":[{"id":"call_123","name":"read","arguments":{"filePath":"hello.txt"}}]}'
        )

        remaining_text, tool_uses = parse_tool_calls(text)

        self.assertEqual("I will inspect the file.", remaining_text)
        self.assertEqual(
            [
                {
                    "type": "tool_use",
                    "id": "call_123",
                    "name": "read",
                    "input": {"filePath": "hello.txt"},
                }
            ],
            tool_uses,
        )

    def test_parse_tool_calls_extracts_multiple_json_tool_calls(self) -> None:
        text = (
            '{"tool_calls":['
            '{"name":"glob","arguments":{"path":".","pattern":"**/*.py"}},'
            '{"name":"grep","arguments":{"path":".","pattern":"TODO"}}'
            ']}'
        )

        remaining_text, tool_uses = parse_tool_calls(text)

        self.assertEqual("", remaining_text)
        self.assertEqual(2, len(tool_uses))
        self.assertEqual("glob", tool_uses[0]["name"])
        self.assertEqual({"path": ".", "pattern": "**/*.py"}, tool_uses[0]["input"])
        self.assertEqual("grep", tool_uses[1]["name"])
        self.assertEqual({"path": ".", "pattern": "TODO"}, tool_uses[1]["input"])


if __name__ == "__main__":
    unittest.main()
