"""Bounded HTTPS access to application-preset research services.

This is deliberately separate from arbitrary-URL retrieval with validated,
pinned public IPs. The small, immutable origin list is a trust decision about
these publishers/search services and the user's configured HTTPS proxy/DNS.
It is not a general SSRF-safe proxy: neither users nor model outputs can add an
origin or change authority through credentials. Publisher redirects remain
inside the fixed publisher list; search credentials never follow redirects.
TLS verification stays enabled. Unknown sources must use the normal public-IP
validation path instead of falling back to this transport.
"""

from __future__ import annotations

import asyncio
from types import MappingProxyType
from urllib.parse import urlsplit

import httpx

from .web_content import (
    MAX_WEB_BYTES,
    WebFetchError,
    WebResponse,
    read_bounded_body,
    require_success,
)


MAX_TRUSTED_BYTES = MAX_WEB_BYTES
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


async def get_trusted_response(url: str, headers: dict | None = None) -> WebResponse:
    """Read preset HTTPS sources with bounded, same-allowlist redirects only.

    Search credentials never follow redirects. Publisher redirects are accepted
    only when the destination is also an immutable preset publisher origin.
    Unknown destinations never inherit this proxy-aware transport.
    """
    target, hostname = _validated_url(url)
    request_headers = _request_headers(hostname, headers)
    redirects = []
    async with asyncio.timeout(MAX_TRUSTED_SECONDS):
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15, connect=10),
            trust_env=True,
            verify=True,
            follow_redirects=False,
        ) as client:
            for hop in range(5):
                async with client.stream(
                    "GET", target, headers=request_headers
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        from urllib.parse import urljoin

                        location = response.headers.get("location")
                        if hostname not in _PUBLISHERS or not location or hop == 4:
                            raise WebFetchError(
                                "预设资料来源重定向不可用或次数过多。",
                                code="redirect_blocked",
                                status_code=response.status_code,
                            )
                        try:
                            following, next_host = _validated_url(
                                urljoin(str(target), location)
                            )
                        except ValueError as exc:
                            raise WebFetchError(
                                "预设资料来源重定向到非预设地址；请单独读取其正式公开链接。",
                                code="redirect_blocked",
                                status_code=response.status_code,
                            ) from exc
                        if next_host not in _PUBLISHERS:
                            raise WebFetchError(
                                "预设资料来源重定向目标不可用。",
                                code="redirect_blocked",
                                status_code=response.status_code,
                            )
                        redirects.append(
                            {
                                "url": str(target),
                                "status_code": response.status_code,
                                "location": str(following),
                            }
                        )
                        client.cookies.clear()
                        target, hostname = following, next_host
                        request_headers = _request_headers(
                            hostname,
                            {"Accept": request_headers.get("Accept", "text/html")},
                        )
                        continue
                    require_success(response.status_code)
                    return WebResponse(
                        str(target),
                        await read_bounded_body(response, MAX_TRUSTED_BYTES),
                        response.headers.get("content-type", "text/html"),
                        response.status_code,
                        redirects,
                    )
    raise WebFetchError("资料来源无法读取。")


async def get_trusted(url: str, headers: dict | None = None) -> tuple[str, bytes, str]:
    """Backward-compatible source transport for search and existing callers."""
    response = await get_trusted_response(url, headers)
    return response.url, response.body, response.content_type
