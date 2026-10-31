import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from jarvis.storage import Store
from jarvis.tools import MAX_FILE_BYTES, MAX_WEB_BYTES, ToolRegistry


def address(value):
    family = socket.AF_INET6 if ":" in value else socket.AF_INET
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (value, 443))]


class ToolsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / "data")
        self.registry = ToolRegistry(self.root / "workspace", self.store)

    def tearDown(self):
        self.temporary.cleanup()

    def symlink(self, link, target, *, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except OSError as exc:
            if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
                self.skipTest(
                    "Windows account lacks symlink privilege; traversal tests still run"
                )
            raise

    async def test_rejects_paths_outside_workspace(self):
        for path in (
            "../secret.txt",
            str(self.root / "secret.txt"),
            "folder/../../secret.txt",
            "folder\\secret.txt",
            ".",
        ):
            with self.subTest(path=path):
                result = await self.registry.execute(
                    "write_file", {"path": path, "content": "secret"}
                )
                self.assertIn("error", result)
        self.assertFalse((self.root / "secret.txt").exists())

    async def test_rejects_windows_device_names_on_every_platform(self):
        names = (
            "NUL.txt",
            "con",
            "Con.md",
            "CON .csv",
            "AUX.backup.md",
            "prn.txt",
            "COM1.txt",
            "com9.csv",
            "LPT1.txt",
            "lpt9.md",
            "COM¹.txt",
            "LPT².md",
            "COM³.csv",
            "CONIN$.txt",
            "CONOUT$",
            "CLOCK$.md",
            "reports/NUL.txt",
            "CON/reports/result.md",
            "reports/LPT1.csv/result.md",
        )
        for path in names:
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    self.registry.resolve_path(path)
                result = await self.registry.execute(
                    "write_file", {"path": path, "content": "must not write"}
                )
                self.assertIn("error", result)
        self.assertEqual(list(self.registry.workspace.iterdir()), [])

    async def test_rejects_streams_and_windows_path_normalization_aliases(self):
        ordinary = self.registry.workspace / "report.md"
        ordinary.write_text("unchanged", encoding="utf-8")
        paths = (
            "report.md:secret",
            "report.md::$DATA",
            "folder:$INDEX_ALLOCATION/report.md",
            "C:report.md",
            "C:/report.md",
            "report.md.",
            "report.md ",
            "folder. /report.md",
            "folder./report.md",
            "folder /report.md",
            ".. /report.md",
            "report\x01.md",
            "report\n.md",
            "report\x7f.md",
            "report?.md",
            "report*.md",
            'report".md',
            "report<.md",
            "report>.md",
            "report|.md",
        )
        for path in paths:
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    self.registry.resolve_path(path)
                for name, arguments in (
                    ("read_file", {"path": path}),
                    (
                        "write_file",
                        {"path": path, "content": "changed", "overwrite": True},
                    ),
                ):
                    self.assertIn("error", await self.registry.execute(name, arguments))
        self.assertEqual(ordinary.read_text(encoding="utf-8"), "unchanged")
        self.assertEqual(list(self.registry.workspace.iterdir()), [ordinary])

    async def test_portable_chinese_space_and_nonreserved_names_remain_usable(self):
        for path in (
            "项目资料/销售 报告.md",
            "reports/console.md",
            "reports/auxiliary.txt",
            "reports/COM10.txt",
            "reports/LPT10.csv",
            "reports/NUL-safe.md",
            "./reports/普通报告.md",
        ):
            with self.subTest(path=path):
                result = await self.registry.execute(
                    "write_file", {"path": path, "content": "真实内容"}
                )
                self.assertNotIn("error", result)
                result = await self.registry.execute("read_file", {"path": path})
                self.assertEqual(result["content"], "真实内容")

    async def test_rejects_symlink_read_write_and_upload_resolution(self):
        outside = self.root / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        self.symlink(self.registry.workspace / "link.txt", outside)
        for name, arguments in (
            ("read_file", {"path": "link.txt"}),
            (
                "write_file",
                {"path": "link.txt", "content": "changed", "overwrite": True},
            ),
        ):
            self.assertIn("error", await self.registry.execute(name, arguments))
        with self.assertRaises(ValueError):
            self.registry.resolve_path("link.txt")
        self.assertEqual(outside.read_text(), "outside")
        self.assertEqual(self.registry.dashboard()["files"], [])

    async def test_rejects_symlink_parent(self):
        self.symlink(self.registry.workspace / "external", self.root, directory=True)
        result = await self.registry.execute(
            "write_file", {"path": "external/outside.md", "content": "x"}
        )
        self.assertIn("error", result)

    async def test_document_creation_and_explicit_overwrite(self):
        args = {"path": "reports/调研.md", "content": "# 调研\n来自真实资料。"}
        result = await self.registry.execute("write_file", args)
        self.assertTrue(result["saved"])
        self.assertEqual(result["path"], args["path"])
        self.assertEqual(
            (await self.registry.execute("read_file", {"path": args["path"]}))[
                "content"
            ],
            args["content"],
        )
        self.assertIn(
            "error",
            await self.registry.execute("write_file", {**args, "content": "覆盖"}),
        )
        self.assertEqual(
            (await self.registry.execute("read_file", {"path": args["path"]}))[
                "content"
            ],
            args["content"],
        )
        self.assertTrue(
            (
                await self.registry.execute(
                    "write_file", {**args, "content": "更新", "overwrite": True}
                )
            )["saved"]
        )
        self.assertEqual(
            (await self.registry.execute("read_file", {"path": args["path"]}))[
                "content"
            ],
            "更新",
        )

    async def test_read_limits_and_utf8_errors(self):
        file = self.registry.workspace / "long.txt"
        file.write_text("x" * 30_100)
        result = await self.registry.execute("read_file", {"path": "long.txt"})
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["content"]), 30_000)
        file.write_bytes(b"x" * (MAX_FILE_BYTES + 1))
        self.assertIn(
            "error", await self.registry.execute("read_file", {"path": "long.txt"})
        )
        file.write_bytes(b"\xff\xfe")
        self.assertIn(
            "UTF-8",
            (await self.registry.execute("read_file", {"path": "long.txt"}))["error"],
        )

    async def test_schema_rejects_invalid_or_unknown_arguments(self):
        cases = [
            ("unknown", {}),
            (["invalid"], {}),
            ("write_file", {"path": "report.md"}),
            ("current_time", {"ignored": "bad"}),
            ("current_time", []),
            ("complete_todo", {"id": True}),
            ("complete_todo", {"id": 0}),
            ("read_file", {"path": 1}),
            ("search_web", {"query": "  "}),
        ]
        for name, arguments in cases:
            with self.subTest(name=name, arguments=arguments):
                self.assertIn("error", await self.registry.execute(name, arguments))

    async def test_csv_statistics_missing_and_text_values(self):
        (self.registry.workspace / "sales.csv").write_text(
            "地区,金额,数量\n上海,12.5,2\n北京,7.5,3\n广州,,1\n", encoding="utf-8-sig"
        )
        result = await self.registry.execute("analyze_csv", {"path": "sales.csv"})
        self.assertEqual(result["row_count"], 3)
        self.assertEqual(result["missing_values"], {"地区": 0, "金额": 1, "数量": 0})
        self.assertEqual(
            result["numeric_summary"]["金额"],
            {"count": 2, "sum": 20.0, "min": 7.5, "max": 12.5, "mean": 10.0},
        )
        self.assertNotIn("地区", result["numeric_summary"])
        self.assertFalse(result["truncated"])

    async def test_csv_duplicate_headers_and_non_finite_values(self):
        (self.registry.workspace / "input.csv").write_text(
            "amount,amount,\n1,NaN,3\n2,inf,4\n"
        )
        result = await self.registry.execute("analyze_csv", {"path": "input.csv"})
        self.assertEqual(result["columns"], ["amount", "amount_2", "column_3"])
        self.assertNotIn("amount_2", result["numeric_summary"])
        json.dumps(result, allow_nan=False)

    async def test_csv_overflow_has_json_safe_statistics(self):
        (self.registry.workspace / "huge.csv").write_text("amount\n1e308\n1e308\n")
        result = await self.registry.execute("analyze_csv", {"path": "huge.csv"})
        self.assertIsNone(result["numeric_summary"]["amount"]["sum"])
        json.dumps(result, allow_nan=False)

    async def test_csv_row_limit_is_reported(self):
        (self.registry.workspace / "input.csv").write_text("amount\n1\n2\n3\n")
        with patch("jarvis.tools.MAX_CSV_ROWS", 2):
            result = await self.registry.execute("analyze_csv", {"path": "input.csv"})
        self.assertEqual(result["row_count"], 2)
        self.assertTrue(result["truncated"])
        self.assertTrue(result["warnings"])

    async def test_persistence_and_clear_does_not_delete_work(self):
        self.store.add_message("user", "写一份报告")
        self.store.add_message("assistant", "已完成")
        await self.registry.execute(
            "save_note", {"title": "来源", "content": "https://example.com"}
        )
        todo = await self.registry.execute("add_todo", {"title": "检查数据"})
        result = await self.registry.execute(
            "complete_todo", {"id": todo["todo"]["id"]}
        )
        self.assertTrue(result["todo"]["done"])
        reopened = Store(self.root / "data")
        self.assertEqual(
            reopened.history(),
            [
                {"role": "user", "content": "写一份报告"},
                {"role": "assistant", "content": "已完成"},
            ],
        )
        self.assertTrue(reopened.list_todos()[0]["done"])
        reopened.clear_history()
        self.assertEqual(reopened.history(), [])
        self.assertEqual(reopened.list_notes()[0]["title"], "来源")
        self.assertEqual(len(reopened.list_todos()), 1)

    async def test_missing_todo_is_structured_error(self):
        self.assertIn(
            "error", await self.registry.execute("complete_todo", {"id": 999})
        )

    async def test_public_target_blocks_private_addresses(self):
        for value in (
            "127.0.0.1",
            "10.0.0.1",
            "169.254.169.254",
            "192.168.1.1",
            "::1",
            "::ffff:127.0.0.1",
            "224.0.0.1",
        ):
            with (
                self.subTest(value=value),
                patch("jarvis.tools.socket.getaddrinfo", return_value=address(value)),
            ):
                with self.assertRaises(ValueError):
                    await self.registry._public_target("https://example.com/page")

    async def test_public_target_rejects_mixed_public_private_dns(self):
        with patch(
            "jarvis.tools.socket.getaddrinfo",
            return_value=address("1.1.1.1") + address("127.0.0.1"),
        ):
            with self.assertRaises(ValueError):
                await self.registry._public_target("https://example.com/")

    async def test_public_target_pins_ip_and_keeps_hostname(self):
        with patch("jarvis.tools.socket.getaddrinfo", return_value=address("1.1.1.1")):
            target, host, sni = await self.registry._public_target(
                "https://example.com/path?q=abc#heading"
            )
        self.assertEqual(str(target), "https://1.1.1.1/path?q=abc")
        self.assertEqual(host, "example.com")
        self.assertEqual(sni, "example.com")

    async def test_public_target_rejects_credentials_ports_and_protocols(self):
        for url in (
            "file:///etc/passwd",
            "https://user:pass@example.com",
            "http://example.com:8080",
            "http://",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                await self.registry._public_target(url)

    async def test_fetch_redirect_to_private_network_is_blocked(self):
        def resolve(host, *args):
            return address("1.1.1.1" if host == "public.example" else "127.0.0.1")

        def respond(request):
            return httpx.Response(
                302, headers={"location": "http://internal.example/secret"}
            )

        actual_client = httpx.AsyncClient

        def mock_client(**kwargs):
            return actual_client(**kwargs, transport=httpx.MockTransport(respond))

        with (
            patch("jarvis.tools.socket.getaddrinfo", side_effect=resolve),
            patch("jarvis.tools.httpx.AsyncClient", side_effect=mock_client),
        ):
            result = await self.registry.execute(
                "fetch_page", {"url": "https://public.example/"}
            )
        self.assertIn("非公网", result["error"])

    async def test_fetch_preserves_host_sni_and_filters_html(self):
        def respond(request):
            self.assertEqual(request.url.host, "1.1.1.1")
            self.assertEqual(request.headers["host"], "public.example")
            self.assertEqual(request.extensions["sni_hostname"], "public.example")
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                content="<title>资料</title><p>可验证的正文</p><script>隐藏指令</script>",
            )

        actual_client = httpx.AsyncClient

        def mock_client(**kwargs):
            self.assertFalse(kwargs["trust_env"])
            self.assertFalse(kwargs["follow_redirects"])
            return actual_client(**kwargs, transport=httpx.MockTransport(respond))

        with (
            patch("jarvis.tools.socket.getaddrinfo", return_value=address("1.1.1.1")),
            patch("jarvis.tools.httpx.AsyncClient", side_effect=mock_client),
        ):
            result = await self.registry.execute(
                "fetch_page", {"url": "https://public.example/article"}
            )
        self.assertEqual(result["url"], "https://public.example/article")
        self.assertEqual(result["title"], "资料")
        self.assertIn("可验证的正文", result["content"])
        self.assertNotIn("隐藏指令", result["content"])

    async def test_fetch_without_content_length_enforces_stream_limit(self):
        class BytesStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"x" * MAX_WEB_BYTES
                yield b"x"

        def respond(request):
            return httpx.Response(200, stream=BytesStream())

        actual_client = httpx.AsyncClient

        def mock_client(**kwargs):
            return actual_client(**kwargs, transport=httpx.MockTransport(respond))

        with (
            patch("jarvis.tools.socket.getaddrinfo", return_value=address("1.1.1.1")),
            patch("jarvis.tools.httpx.AsyncClient", side_effect=mock_client),
        ):
            result = await self.registry.execute(
                "fetch_page", {"url": "https://public.example/article"}
            )
        self.assertIn("1 MB", result["error"])

    async def test_search_reports_challenge_without_fake_results(self):
        async def page(*args, **kwargs):
            return (
                "https://html.duckduckgo.com",
                b'<form id="challenge-form">Bots use DuckDuckGo too</form>',
                "text/html",
            )

        with patch.object(self.registry, "_public_get", side_effect=page):
            result = await self.registry.execute("search_web", {"query": "research"})
        self.assertIn("人机验证", result["error"])
        self.assertNotIn("results", result)

    async def test_search_extracts_real_unwrapped_source_links(self):
        async def page(*args, **kwargs):
            return (
                "https://html.duckduckgo.com",
                b'<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpaper">Example <b>paper</b></a><a class="result__snippet">Actual abstract</a>',
                "text/html",
            )

        with patch.object(self.registry, "_public_get", side_effect=page):
            result = await self.registry.execute("search_web", {"query": "research"})
        self.assertEqual(
            result["results"],
            [
                {
                    "title": "Example paper",
                    "url": "https://example.com/paper",
                    "snippet": "Actual abstract",
                }
            ],
        )

    async def test_brave_auth_is_not_forwarded_through_redirects(self):
        self.registry.brave_api_key = "test-secret"

        async def page(url, headers, *, allow_redirects):
            self.assertTrue(url.startswith("https://api.search.brave.com/"))
            self.assertEqual(headers["X-Subscription-Token"], "test-secret")
            self.assertFalse(allow_redirects)
            return (
                url,
                b'{"web":{"results":[{"title":"Paper","url":"https://example.com"}]}}',
                "application/json",
            )

        with patch.object(self.registry, "_public_get", side_effect=page):
            result = await self.registry.execute("search_web", {"query": "research"})
        self.assertEqual(result["provider"], "brave")
        self.assertEqual(result["results"][0]["url"], "https://example.com")


if __name__ == "__main__":
    unittest.main()
