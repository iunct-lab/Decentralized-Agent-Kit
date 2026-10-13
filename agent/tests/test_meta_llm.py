import unittest
from unittest.mock import MagicMock, patch

from dak_agent.meta_llm import complete_json, extract_json, strip_think_tags


class TestExtractJson(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})

    def test_json_with_surrounding_prose(self):
        text = 'Here is the config:\n```json\n{"instruction": "x", "selected_tools": []}\n``` Hope it helps!'
        self.assertEqual(extract_json(text), {"instruction": "x", "selected_tools": []})

    def test_empty_text(self):
        self.assertEqual(extract_json(""), {})

    def test_garbage_text(self):
        self.assertEqual(extract_json("not json at all"), {})


class TestStripThinkTags(unittest.TestCase):
    def test_strip_think_tags_removes_reasoning_block(self):
        text = '<think>the user wants {"a": 0}\nso…</think>{"a": 1}'
        self.assertEqual(strip_think_tags(text), '{"a": 1}')

    def test_text_without_think_is_unchanged(self):
        self.assertEqual(strip_think_tags('{"a": 1}'), '{"a": 1}')


class TestCompleteJson(unittest.TestCase):
    def _mock_response(self, content: str):
        response = MagicMock()
        response.choices[0].message.content = content
        return response

    @patch("litellm.completion")
    def test_returns_parsed_json(self, mock_completion):
        mock_completion.return_value = self._mock_response('{"instruction": "do x"}')
        result = complete_json("gemini/gemini-2.5-flash", "prompt")
        self.assertEqual(result, {"instruction": "do x"})
        self.assertEqual(mock_completion.call_args.kwargs["response_format"], {"type": "json_object"})

    @patch("litellm.completion")
    def test_complete_json_strips_think_before_parsing(self, mock_completion):
        # The braces inside the reasoning would otherwise make the greedy
        # fallback span from the first "{" to the last "}" and fail to parse.
        mock_completion.return_value = self._mock_response(
            '<think>I should return {"instruction": ...}</think>'
            '{"instruction": "x", "selected_tools": [], "selected_skills": []}'
        )
        result = complete_json("ollama_chat/qwen3:8b", "prompt")
        self.assertEqual(result, {"instruction": "x", "selected_tools": [], "selected_skills": []})

    @patch("litellm.completion")
    def test_retries_without_json_mode(self, mock_completion):
        # First call (JSON mode) fails, second (plain) succeeds
        mock_completion.side_effect = [
            Exception("provider does not support response_format"),
            self._mock_response('{"a": 1}'),
        ]
        result = complete_json("some/model", "prompt")
        self.assertEqual(result, {"a": 1})
        self.assertEqual(mock_completion.call_count, 2)

    @patch("litellm.completion")
    def test_total_failure_returns_empty(self, mock_completion):
        mock_completion.side_effect = Exception("down")
        self.assertEqual(complete_json("some/model", "prompt"), {})


if __name__ == "__main__":
    unittest.main()
