import json
import unittest

from jarvis.tool_output import model_tool_result


class ToolOutputTests(unittest.TestCase):
    def test_large_escaped_page_keeps_usable_cursor_and_valid_budget(self):
        content = '中文\\"\n' * 8000
        page = {
            "url": "https://example.com/article",
            "page_id": "page_123",
            "offset": 12000,
            "next_offset": None,
            "content": content,
            "total_chars": 12000 + len(content),
            "truncated": False,
            "links": ["https://example.com/" + "x" * 2000] * 40,
        }
        value = model_tool_result(page)
        self.assertLessEqual(len(json.dumps(value, ensure_ascii=False)), 24000)
        self.assertEqual(value["page_id"], page["page_id"])
        self.assertEqual(value["url"], page["url"])
        self.assertGreater(len(value["content"]), 0)
        self.assertLess(len(value["content"]), len(content))
        self.assertTrue(content.startswith(value["content"]))
        self.assertEqual(value["next_offset"], page["offset"] + len(value["content"]))
        self.assertTrue(value["truncated"])
        self.assertEqual(page["content"], content)
        self.assertIsNone(page["next_offset"])

    def test_large_links_do_not_invent_a_next_page_when_content_is_complete(self):
        value = model_tool_result(
            {
                "content": "完整正文",
                "page_id": "page_123",
                "offset": 0,
                "next_offset": None,
                "truncated": False,
                "links": ["x" * 5000] * 10,
            }
        )
        self.assertEqual(value["content"], "完整正文")
        self.assertIsNone(value["next_offset"])
        self.assertFalse(value["truncated"])
        self.assertTrue(value["model_context_truncated"])

    def test_other_large_results_stay_valid_json_and_small_results_are_intact(self):
        original = {"error": "暂时不可用", "status_code": 503}
        self.assertIs(model_tool_result(original), original)
        result = model_tool_result({"rows": ['\\\n"' * 5000] * 10})
        encoded = json.dumps(result, ensure_ascii=False)
        self.assertLessEqual(len(encoded), 24000)
        self.assertTrue(json.loads(encoded)["truncated"])
