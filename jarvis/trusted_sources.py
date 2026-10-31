"""Bounded HTTPS access to application-preset research services.

This is deliberately separate from arbitrary-URL retrieval with validated,
pinned public IPs. The small, immutable origin list is a trust decision about
these publishers/search services and the user's configured HTTPS proxy/DNS.
It is not a general SSRF-safe proxy: neither users nor model outputs can add an
origin, credentials cannot change the authority, and redirects are never sent.
TLS verification stays enabled. Unknown sources must use the normal public-IP
validation path instead of falling back to this transport.
"""

from __future__ import annotations

import asyncio
from types import MappingProxyType
from urllib.parse import urlsplit

import httpx


MAX_TRUSTED_BYTES = 1024 * 1024
MAX_TRUSTED_SECONDS = 25
_PUBLISHERS = frozenset(
    {
        "news.un.org",
        "www.federalreserve.gov",
        "www.ecb.europa.eu",
        "www.imf.org",
        "www.worldbank.org",
    }
)
_SEARCH_ENDPOINTS = MappingProxyType(
    {
        "html.duckduckgo.com": "/html/",
        "api.search.brave.com": "/res/v1/web/search",
    }
)


def _validated_url(url: str) -> tuple[httpx.URL, str]:
    if (
        not isinstance(url, str)
        or not url
        or len(url) > 4096
        or "\\" in url
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in url
        )
    ):
        raise ValueError("资料来源 URL 格式无效")
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
        ):
            raise ValueError("资料来源必须使用无登录凭据的 HTTPS 默认端口")
        hostname = parsed.hostname.lower()
        if hostname in _SEARCH_ENDPOINTS:
            if parsed.path != _SEARCH_ENDPOINTS[hostname]:
                raise ValueError("不支持该搜索服务路径")
        elif hostname not in _PUBLISHERS:
            raise ValueError("该域名不属于应用预设资料来源")
        canonical = httpx.URL(url).copy_with(fragment=None)
        # Keep URL parsers in agreement; never accept a normalized different host.
        if canonical.host != hostname or canonical.scheme != "https":
            raise ValueError("资料来源 URL 解析不一致")
        return canonical, hostname
    except (httpx.InvalidURL, UnicodeError) as exc:
        raise ValueError("资料来源 URL 格式无效") from exc


def supports_trusted_url(url: str) -> bool:
    """Whether this exact HTTPS origin/path has an application-preset route."""
    try:
        _validated_url(url)
    except (ValueError, TypeError):
        return False
    return True


def _request_headers(hostname: str, headers: dict | None) -> dict:
    selected = {
        "User-Agent": "Open-JARVIS/0.1 (local research assistant)",
        "Accept-Encoding": "identity",
    }
    if headers is None:
        return selected
    if not isinstance(headers, dict):
        raise ValueError("资料请求头格式无效")
    for key, value in headers.items():
        if (
            not isinstance(key, str)
            or not isinstance(value, str)
            or len(value) > 8192
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("资料请求头格式无效")
        lowered = key.lower()
        if lowered == "accept":
            selected["Accept"] = value
        elif lowered == "x-subscription-token" and hostname == "api.search.brave.com":
            selected["X-Subscription-Token"] = value
        else:
            # Do not accept Host, proxy credentials or arbitrary auth headers.
            raise ValueError("该资料来源不支持所给请求头")
    return selected


async def get_trusted(url: str, headers: dict | None = None) -> tuple[str, bytes, str]:
    """Read one preset HTTPS endpoint; no redirect, cookie reuse or IP fallback."""
    target, hostname = _validated_url(url)
    request_headers = _request_headers(hostname, headers)
    async with asyncio.timeout(MAX_TRUSTED_SECONDS):
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15, connect=10),
            trust_env=True,
            verify=True,
            follow_redirects=False,
        ) as client:
            async with client.stream(
                "GET", target, headers=request_headers
            ) as response:
                if 300 <= response.status_code < 400:
                    raise ValueError(
                        "预设资料来源返回重定向，已停止读取；请使用其正式页面地址"
                    )
                if not 200 <= response.status_code < 300:
                    raise ValueError(f"资料来源请求失败，HTTP {response.status_code}")
                encoding = response.headers.get("content-encoding", "identity").lower()
                if encoding not in {"", "identity"}:
                    raise ValueError("资料来源忽略了无压缩请求，暂不支持读取")
                length = response.headers.get("content-length", "")
                if length.isdigit() and int(length) > MAX_TRUSTED_BYTES:
                    raise ValueError("资料超过 1 MB 读取限制")
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                    body.extend(chunk)
                    if len(body) > MAX_TRUSTED_BYTES:
                        raise ValueError("资料超过 1 MB 读取限制")
                return (
                    str(target),
                    bytes(body),
                    response.headers.get("content-type", "text/html"),
                )
