"""Bounded web response decoding and readable, source-preserving extraction.

No browser, document model, third-party parser or arbitrary code is executed.
Fetched text remains untrusted reference material, including text in links.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass, field
from html.parser import HTMLParser
import re
from urllib.parse import quote, urljoin, urlsplit
import zlib


MAX_WEB_BYTES = 4 * 1024 * 1024
MAX_EXTRACTED_CHARS = 200_000


class WebFetchError(ValueError):
    def __init__(
        self, message, *, code="fetch_failed", status_code=None, retryable=False
    ):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable

    def result(self):
        return {
            "error": str(self),
            "error_code": self.code,
            "status_code": self.status_code,
            "retryable": self.retryable,
        }


@dataclass
class WebResponse:
    url: str
    body: bytes
    content_type: str
    status_code: int
    redirects: list[dict] = field(default_factory=list)


def require_success(status_code):
    if 200 <= status_code < 300:
        return
    hints = {
        401: "该网页需要登录，请提供公开来源。",
        403: "该来源拒绝自动访问；请换用其他公开来源。",
        404: "网页不存在，请核对链接或重新搜索。",
        429: "该来源请求过于频繁，请稍后重试或换用其他来源。",
    }
    raise WebFetchError(
        f"网页请求失败，HTTP {status_code}。"
        + hints.get(status_code, "来源服务暂不可用，请换用其他来源。"),
        code={
            401: "login_required",
            403: "access_denied",
            404: "not_found",
            429: "rate_limited",
        }.get(status_code, "http_error"),
        status_code=status_code,
        retryable=status_code == 429 or status_code >= 500,
    )


async def read_bounded_body(response, limit=MAX_WEB_BYTES):
    """Bound both wire and decoded bytes, including a compressed response bomb."""
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in {"", "identity", "gzip", "x-gzip", "deflate"}:
        raise WebFetchError("网页使用暂不支持的压缩格式。", code="unsupported_encoding")
    length = response.headers.get("content-length", "")
    if length.isdigit() and int(length) > limit:
        raise WebFetchError(
            "网页超过 4 MB 读取限制，请选择更具体的文章链接。", code="body_too_large"
        )
    decoder = None
    if encoding in {"gzip", "x-gzip"}:
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    elif encoding == "deflate":
        decoder = zlib.decompressobj()
    body, wire_bytes = bytearray(), 0
    # Mock/test clients may supply an already consumed response. Its content is
    # already decoded by HTTPX; production streamed responses take the raw path.
    if response.is_stream_consumed:
        value = response.content
        if len(value) > limit:
            raise WebFetchError(
                "网页超过 4 MB 读取限制，请选择更具体的文章链接。",
                code="body_too_large",
            )
        return value
    try:
        async for chunk in response.aiter_raw(chunk_size=64 * 1024):
            wire_bytes += len(chunk)
            if wire_bytes > limit:
                raise WebFetchError(
                    "网页超过 4 MB 读取限制，请选择更具体的文章链接。",
                    code="body_too_large",
                )
            if decoder:
                try:
                    value = decoder.decompress(chunk, limit - len(body) + 1)
                except zlib.error:
                    if encoding != "deflate" or body or wire_bytes != len(chunk):
                        raise
                    # Some servers label a raw DEFLATE stream as deflate.
                    decoder = zlib.decompressobj(-zlib.MAX_WBITS)
                    value = decoder.decompress(chunk, limit + 1)
            else:
                value = chunk
            body.extend(value)
            if len(body) > limit or (decoder and decoder.unconsumed_tail):
                raise WebFetchError(
                    "网页解压后超过 4 MB 读取限制。", code="body_too_large"
                )
        if decoder:
            body.extend(decoder.flush(limit - len(body) + 1))
            if len(body) > limit:
                raise WebFetchError(
                    "网页解压后超过 4 MB 读取限制。", code="body_too_large"
                )
            if not decoder.eof or decoder.unused_data:
                raise WebFetchError(
                    "网页压缩数据不完整或格式不受支持。", code="invalid_compression"
                )
    except zlib.error as exc:
        raise WebFetchError(
            "网页压缩数据损坏，无法读取正文。", code="invalid_compression"
        ) from exc
    return bytes(body)


def decode_text(body, content_type):
    """BOM, declared header/meta encoding, UTF-8, then common Chinese encoding."""
    candidates = []
    if body.startswith(codecs.BOM_UTF8):
        candidates.append("utf-8-sig")
    elif body.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        candidates.append("utf-16")
    declared = re.search(r"charset\s*=\s*[\"']?([a-zA-Z0-9_.-]+)", content_type, re.I)
    if declared:
        candidates.append(declared[1])
    # Restrict sniffing to tags rather than accepting arbitrary article text.
    prefix = body[:8192].decode("ascii", errors="ignore")
    for tag in re.findall(r"<meta\b[^>]{0,1024}>", prefix, flags=re.I):
        match = re.search(r"charset\s*=\s*[\"']?([a-zA-Z0-9_.-]+)", tag, re.I)
        if match:
            candidates.append(match[1])
            break
    candidates.extend(("utf-8", "gb18030"))
    checked = set()
    for name in candidates:
        try:
            name = codecs.lookup(name).name
            if name in checked:
                continue
            checked.add(name)
            return body.decode(name), name, []
        except (LookupError, UnicodeError):
            continue
    return (
        body.decode("utf-8", errors="replace"),
        "utf-8",
        ["部分字符无法可靠解码，已用替代字符标记。"],
    )


def _public_link(base_url, href):
    if not href or len(href) > 2048 or any(ord(c) < 32 for c in href):
        return None
    try:
        value = urljoin(base_url, href)
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            return None
        value = quote(value, safe=":/?#[]@!$&'()*+,;=%")
        return value if len(value) <= 2048 else None
    except ValueError:
        return None


class _Parts:
    def __init__(self):
        self.values = []
        self.size = 0
        self.truncated = False

    def append(self, text):
        remaining = MAX_EXTRACTED_CHARS - self.size
        if len(text) > remaining:
            self.truncated = True
        if remaining > 0:
            text = text[:remaining]
            self.values.append(text)
            self.size += len(text)

    def text(self):
        lines = [
            re.sub(r"[\t \f\v]+", " ", line).strip()
            for line in "".join(self.values).replace("\r", "").split("\n")
        ]
        text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
        text = re.sub(r"\[\s*\]\(https?://[^)\s]+\)", "", text)
        text = re.sub(r"(?im)^\[skip (?:to|navigation)[^\]]*\]\([^)]*\)\s*", "", text)
        return re.sub(r"\n{3,}", "\n\n", text).strip()


class PageParser(HTMLParser):
    _VOID = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
    _SKIP = {
        "script",
        "style",
        "noscript",
        "svg",
        "template",
        "nav",
        "aside",
        "footer",
        "form",
        "button",
    }
    _BLOCK = {
        "p",
        "div",
        "section",
        "article",
        "main",
        "blockquote",
        "ul",
        "ol",
        "table",
        "tr",
        "dl",
        "dt",
        "dd",
        "pre",
    }

    def __init__(self, url, format):
        super().__init__(convert_charrefs=True)
        self.url, self.format = url, format
        self.stack = []
        self.parts, self.main_parts, self.article_parts = _Parts(), _Parts(), _Parts()
        self.title_parts = []
        self.meta_title = ""
        self.published_at = ""
        self.links = []
        self._seen_links = set()
        self.has_scripts = False
        self.node_count = 0

    def _inside(self, tag):
        if tag == "main":
            return any(
                frame[0] == "main"
                or (frame[2].get("role") or "").lower() == "main"
                or (frame[2].get("id") or "").lower()
                in {
                    "content",
                    "main",
                    "main-content",
                    "maincontent",
                    "main-wrapper",
                    "main_content",
                }
                for frame in self.stack
            )
        if tag == "article":
            return any(
                frame[0] == "article"
                or (frame[2].get("itemprop") or "").lower() == "articlebody"
                for frame in self.stack
            )
        return any(frame[0] == tag for frame in self.stack)

    def _emit(self, text):
        if any(frame[1] for frame in self.stack):
            return
        self.parts.append(text)
        if self._inside("main"):
            self.main_parts.append(text)
        if self._inside("article"):
            self.article_parts.append(text)

    def handle_starttag(self, tag, attributes):
        self.node_count += 1
        if self.node_count > 60_000:
            raise WebFetchError(
                "网页结构过于复杂，请选择具体文章或纯文本来源。",
                code="document_too_complex",
            )
        attrs = dict(attributes)
        if tag == "a":
            attrs["_jarvis_text"] = False
        if tag == "script":
            self.has_scripts = True
        if tag == "meta":
            key = (attrs.get("property") or attrs.get("name") or "").lower()
            if key in {"og:title", "twitter:title"} and not self.meta_title:
                self.meta_title = (attrs.get("content") or "")[:500]
            if (
                key in {"article:published_time", "datepublished", "date", "pubdate"}
                and not self.published_at
            ):
                self.published_at = (attrs.get("content") or "")[:120]
        hidden = "hidden" in attrs or (attrs.get("aria-hidden") or "").lower() == "true"
        hidden |= bool(
            re.search(
                r"(?:display\s*:\s*none|visibility\s*:\s*hidden)",
                attrs.get("style") or "",
                re.I,
            )
        )
        skip = (
            tag in self._SKIP
            or hidden
            or (attrs.get("role") or "").lower()
            in {
                "navigation",
                "banner",
                "contentinfo",
                "complementary",
                "menu",
                "menubar",
                "search",
            }
        )
        skip |= tag == "head"
        skip |= tag == "header" and not (
            self._inside("main") or self._inside("article")
        )
        landmarks = {
            (attrs.get("id") or "").lower(),
            *(attrs.get("class") or "").lower().split(),
        }
        skip |= bool(
            landmarks
            & {
                "skip-links",
                "mainnav",
                "topnav",
                "navbar",
                "cookie-banner",
                "cookie-consent",
                "cookie-notice",
                "sharedl",
                "newsletter",
                "hidden",
                "d-none",
                "cookieconsent",
                "feedback",
                "breadcrumb",
                "breadcrumbs",
            }
        )
        skip |= any(
            re.search(r"(?:^|[-_])(?:share|sharing)(?:[-_]|$)|^sharethis", marker)
            for marker in landmarks
        )
        if tag == "title":
            skip = True
        if tag not in self._VOID:
            if len(self.stack) >= 256:
                raise WebFetchError(
                    "网页嵌套结构过深，无法可靠提取正文。", code="document_too_complex"
                )
            self.stack.append((tag, skip, attrs))
        if tag == "time" and not self.published_at:
            self.published_at = (attrs.get("datetime") or "")[:120]
        if tag in self._BLOCK or re.fullmatch(r"h[1-6]", tag):
            self._emit("\n\n")
        if re.fullmatch(r"h[1-6]", tag) and self.format == "markdown":
            self._emit("#" * int(tag[1]) + " ")
        if tag == "br":
            self._emit("\n")
        if tag == "li":
            self._emit("\n- " if self.format == "markdown" else "\n• ")
        if tag in {"td", "th"}:
            self._emit(" | ")
        if tag == "a":
            link = _public_link(self.url, attrs.get("href"))
            if link and not any(frame[1] for frame in self.stack):
                if self.format == "markdown":
                    self._emit("[")
        if tag == "pre" and self.format == "markdown":
            self._emit("```\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self._VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        index = next(
            (i for i in range(len(self.stack) - 1, -1, -1) if self.stack[i][0] == tag),
            None,
        )
        if index is None:
            return
        if tag == "a":
            link = _public_link(self.url, self.stack[index][2].get("href"))
            if link and not any(frame[1] for frame in self.stack):
                if (
                    self.stack[index][2]["_jarvis_text"]
                    and link not in self._seen_links
                    and len(self.links) < 20
                    and sum(map(len, self.links)) + len(link) <= 8000
                ):
                    self._seen_links.add(link)
                    self.links.append(link)
                if self.format == "markdown":
                    self._emit(
                        "](" + link.replace("(", "%28").replace(")", "%29") + ")"
                    )
        if tag == "pre" and self.format == "markdown":
            self._emit("\n```")
        if tag in self._BLOCK or re.fullmatch(r"h[1-6]", tag):
            self._emit("\n\n")
        if tag == "li":
            self._emit("\n")
        del self.stack[index:]

    def handle_data(self, data):
        if data.strip() and not any(frame[1] for frame in self.stack):
            for frame in reversed(self.stack):
                if frame[0] == "a":
                    frame[2]["_jarvis_text"] = True
                    break
        if self._inside("title") and sum(map(len, self.title_parts)) < 500:
            self.title_parts.append(data[:500])
        else:
            self._emit(data)

    def result(self):
        # Semantic article/main markup is preferred only when it contains real
        # text; malformed/empty containers do not erase the fallback document.
        selected, extraction = self.parts, "document"
        for parts, name in ((self.main_parts, "main"), (self.article_parts, "article")):
            if len(parts.text()) >= (30 if name == "main" else 80):
                selected, extraction = parts, name
        title = " ".join((self.meta_title or "".join(self.title_parts)).split())[:500]
        return {
            "title": title,
            "content": selected.text(),
            "extraction": extraction,
            "published_at": self.published_at or None,
            "links": self.links,
            "extraction_truncated": selected.truncated,
        }


def extract_content(response, format):
    mime = response.content_type.partition(";")[0].strip().lower()
    allowed = {
        "text/html",
        "application/xhtml+xml",
        "text/plain",
        "text/markdown",
        "text/x-markdown",
        "application/json",
        "application/xml",
        "text/xml",
    }
    if mime not in allowed:
        raise WebFetchError(
            "该链接不是可读取网页；支持 HTML、文本、Markdown、JSON/XML，不支持 PDF、Office 或其他二进制附件。",
            code="unsupported_content_type",
            status_code=response.status_code,
        )
    content, encoding, warnings = decode_text(response.body, response.content_type)
    result = {
        "title": "",
        "content": content[:MAX_EXTRACTED_CHARS],
        "extraction_truncated": len(content) > MAX_EXTRACTED_CHARS,
        "links": [],
        "published_at": None,
        "extraction": "text",
    }
    if mime in {"text/html", "application/xhtml+xml"}:
        parser = PageParser(response.url, format)
        parser.feed(content)
        parser.close()
        result = parser.result()
        challenge = re.search(
            r"(?:just a moment|access denied|verify (?:you are|you're) human|人机验证|安全验证)",
            result["title"],
            re.I,
        )
        if challenge and len(result["content"]) < 2000:
            raise WebFetchError(
                "该网页要求人机验证或拒绝自动读取，请换用公开来源。",
                code="challenge",
                status_code=response.status_code,
            )
        if parser.has_scripts and (
            len(result["content"]) < 100
            or re.search(
                r"(?:enable|requires?|disabled)[^\n]{0,80}javascript|javascript[^\n]{0,80}(?:disabled|required)",
                result["content"],
                re.I,
            )
        ):
            warnings.append(
                "正文很少；页面可能依赖 JavaScript 或登录，当前工具不执行脚本。"
            )
    if not result["content"].strip():
        raise WebFetchError(
            "未提取到可阅读正文；页面可能依赖 JavaScript、登录或返回空响应。",
            code="empty_content",
            status_code=response.status_code,
        )
    if result["extraction_truncated"]:
        warnings.append("正文提取达到 200000 字符上限；分页只能继续读取已提取部分。")
    return {**result, "encoding": encoding, "content_type": mime, "warnings": warnings}
