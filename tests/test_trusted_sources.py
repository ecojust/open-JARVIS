import asyncio
import unittest
from unittest.mock import patch

import httpx

from jarvis.trusted_sources import MAX_TRUSTED_BYTES, get_trusted, supports_trusted_url


class ChunkedBody(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.read_count = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.read_count += 1
            yield chunk


class TrustedSourceTests(unittest.IsolatedAsyncioTestCase):
    def mocked_client(self, handler):
        original = httpx.AsyncClient

        def make_client(**kwargs):
            self.assertTrue(kwargs["trust_env"])
            self.assertTrue(kwargs["verify"])
            self.assertFalse(kwargs["follow_redirects"])
            return original(transport=httpx.MockTransport(handler), **kwargs)

        return patch(
            "jarvis.trusted_sources.httpx.AsyncClient", side_effect=make_client
        )

    async def test_unknown_origins_and_ambiguous_urls_are_rejected_before_network(self):
        bad_urls = [
            "http://www.imf.org/news",
            "https://127.0.0.1/",
            "https://[::1]/",
            "https://10.0.0.1/",
            "https://www.imf.org.example.com/news",
            "https://example.com/www.imf.org",
            "https://evil@www.imf.org/news",
            "https://www.imf.org@127.0.0.1/",
            "https://www.imf.org:8443/news",
            "https://www.imf.org./news",
            "https://www.imf.org\\@127.0.0.1/",
            "https://www.imf.org/\nnews",
            "https://www.imf.org/\tnews",
            " https://www.imf.org/news",
            "https:///news",
            "https://html.duckduckgo.com/redirect?url=https://127.0.0.1",
            "https://html.duckduckgo.com/html/../redirect",
            "https://api.search.brave.com/other",
        ]
        with patch("jarvis.trusted_sources.httpx.AsyncClient") as client:
            for url in bad_urls:
                with self.subTest(url=url):
                    self.assertFalse(supports_trusted_url(url))
                    with self.assertRaises(ValueError):
                        await get_trusted(url)
            client.assert_not_called()

    async def test_official_body_preserves_source_and_does_not_forward_arbitrary_headers(
        self,
    ):
        def handler(request):
            self.assertEqual(request.url.host, "www.federalreserve.gov")
            self.assertEqual(request.headers["accept-encoding"], "identity")
            self.assertNotIn("authorization", request.headers)
            return httpx.Response(
                200,
                content=b"actual source text",
                headers={"content-type": "text/plain; charset=utf-8"},
            )

        url = "https://www.federalreserve.gov/newsevents/pressreleases.htm#main"
        self.assertTrue(supports_trusted_url(url))
        with self.mocked_client(handler):
            actual, body, content_type = await get_trusted(url)
        self.assertEqual(actual, url.partition("#")[0])
        self.assertEqual(body, b"actual source text")
        self.assertEqual(content_type, "text/plain; charset=utf-8")
        with patch("jarvis.trusted_sources.httpx.AsyncClient") as client:
            for headers in (
                {"Host": "127.0.0.1"},
                {"Authorization": "Bearer secret"},
                {"X-Subscription-Token": "secret"},
                {"Accept": "text/html\r\nHost: 127.0.0.1"},
            ):
                with self.assertRaises(ValueError):
                    await get_trusted(url, headers)
            client.assert_not_called()

    async def test_brave_auth_is_never_forwarded_to_redirect_destination(self):
        seen = []

        def handler(request):
            seen.append(request)
            self.assertEqual(request.headers["x-subscription-token"], "test-search-key")
            return httpx.Response(
                302, headers={"location": "https://127.0.0.1/private"}
            )

        with self.mocked_client(handler):
            with self.assertRaisesRegex(ValueError, "重定向"):
                await get_trusted(
                    "https://api.search.brave.com/res/v1/web/search?q=finance",
                    {
                        "X-Subscription-Token": "test-search-key",
                        "Accept": "application/json",
                    },
                )
        self.assertEqual(len(seen), 1)

    async def test_declared_and_streamed_size_are_bounded(self):
        for declared in (True, False):
            with self.subTest(declared=declared):
                stream = ChunkedBody([b"a" * 65536] * (MAX_TRUSTED_BYTES // 65536 + 2))

                def handler(request):
                    headers = (
                        {"content-length": str(MAX_TRUSTED_BYTES + 1)}
                        if declared
                        else {}
                    )
                    return httpx.Response(200, stream=stream, headers=headers)

                with self.mocked_client(handler):
                    with self.assertRaisesRegex(ValueError, "4 MB"):
                        await get_trusted("https://news.un.org/en/story/2026/test")
                self.assertEqual(
                    stream.read_count, 0 if declared else MAX_TRUSTED_BYTES // 65536 + 1
                )

    async def test_compression_and_http_failures_are_explicit(self):
        for code, headers, message in (
            (403, {}, "HTTP 403"),
            (429, {}, "HTTP 429"),
            (200, {"content-encoding": "br"}, "压缩格式"),
        ):
            with self.subTest(code=code, headers=headers):
                with self.mocked_client(
                    lambda request: httpx.Response(
                        code, stream=ChunkedBody([]), headers=headers
                    )
                ):
                    with self.assertRaisesRegex(ValueError, message):
                        await get_trusted("https://www.worldbank.org/en/news")

    async def test_total_timeout_stops_a_stalled_source(self):
        cancelled = asyncio.Event()

        async def handler(request):
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        with (
            self.mocked_client(handler),
            patch("jarvis.trusted_sources.MAX_TRUSTED_SECONDS", 0.02),
        ):
            with self.assertRaises(TimeoutError):
                await get_trusted("https://www.imf.org/en/News")
        self.assertTrue(cancelled.is_set())


if __name__ == "__main__":
    unittest.main()
