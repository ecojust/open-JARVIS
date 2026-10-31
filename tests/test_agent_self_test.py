"""The explicit packaged-runtime check must exercise MAF, not demo commands."""

import builtins
import unittest
from unittest.mock import patch

import httpx

from jarvis.agent_self_test import run_agent_self_test


class AgentSelfTestTests(unittest.TestCase):
    def test_real_local_maf_delegation_writes_chinese_and_ignores_user_provider(self):
        original_client = httpx.AsyncClient
        with patch.dict(
            "os.environ",
            {
                "OPENAI_API_KEY": "do-not-use-user-key",
                "OPENAI_BASE_URL": "https://must-not-contact.example/v1",
                "OPENAI_MODEL": "must-not-use-user-model",
                "HTTP_PROXY": "http://127.0.0.1:1",
                "HTTPS_PROXY": "http://127.0.0.1:1",
                "ALL_PROXY": "http://127.0.0.1:1",
                "NO_PROXY": "",
            },
        ):
            report = run_agent_self_test()
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["model_requests"], 4)
        self.assertEqual(report["tool_calls"], 1)
        self.assertEqual(report["streaming_requests"], 2)
        self.assertTrue(report["coordinator_writer_delegation"])
        self.assertTrue(report["chinese_request_roundtrip"])
        self.assertTrue(report["chinese_file_roundtrip"])
        self.assertTrue(report["chinese_answer_roundtrip"])
        self.assertTrue(report["local_server_stopped"])
        self.assertFalse(report["external_services_used"])
        self.assertFalse(report["user_configuration_used"])
        self.assertIs(httpx.AsyncClient, original_client)

    def test_missing_runtime_module_fails_closed_with_safe_diagnostic(self):
        original_import = builtins.__import__

        def missing_provider(name, *args, **kwargs):
            if name == "agent_framework_openai":
                raise ModuleNotFoundError(
                    "SECRET user credential must not appear",
                    name="agent_framework_openai",
                )
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", missing_provider):
            report = run_agent_self_test()
        self.assertFalse(report["ok"])
        self.assertEqual(report["error_type"], "AgentError")
        self.assertIn("ModuleNotFoundError", report["diagnostic"])
        self.assertIn("agent_framework_openai", report["diagnostic"])
        self.assertNotIn("SECRET", str(report))
        self.assertTrue(report["local_server_stopped"])


if __name__ == "__main__":
    unittest.main()
