"""Provider-specific options are limited to the parsed official endpoint."""

import unittest

from jarvis.provider_options import chat_completion_body_options


class ProviderOptionsTests(unittest.TestCase):
    def test_official_host_supports_root_and_versioned_base_urls(self):
        for url in (
            "https://api.deepseek.com",
            "https://api.deepseek.com/v1",
            "https://API.DEEPSEEK.COM:443/v1/",
        ):
            with self.subTest(url=url):
                self.assertEqual(
                    chat_completion_body_options(url),
                    {"thinking": {"type": "disabled"}},
                )

    def test_other_hosts_and_misleading_urls_keep_provider_defaults(self):
        for url in (
            "https://api.openai.com/v1",
            "http://127.0.0.1:1234/v1",
            "https://openrouter.ai/api/v1",
            "https://api.deepseek.com.example/v1",
            "https://proxy.example/api.deepseek.com",
            "https://proxy.example?host=api.deepseek.com",
            "https://api.deepseek.com@proxy.example/v1",
            "api.deepseek.com",
            "https://[invalid",
            "",
        ):
            with self.subTest(url=url):
                self.assertEqual(chat_completion_body_options(url), {})


if __name__ == "__main__":
    unittest.main()
