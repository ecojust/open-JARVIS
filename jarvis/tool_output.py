"""Bound model-facing tool results without discarding web continuation cursors."""

import json


def _size(value):
    return len(json.dumps(value, ensure_ascii=False))


def _fit_text(result, key, text, limit):
    low, high = 0, min(len(text), limit)
    while low < high:
        middle = (low + high + 1) // 2
        if _size({**result, key: text[:middle]}) <= limit:
            low = middle
        else:
            high = middle - 1
    return text[:low]


def model_tool_result(result: dict, limit: int = 24000) -> dict:
    """Keep raw audit events unchanged; only the model sees this smaller view."""
    if _size(result) <= limit:
        return result
    if isinstance(result.get("content"), str):
        # Prefer complete identity/cursor fields over a large navigation-link
        # list. URL and page_id let the model explicitly request the next page.
        keys = (
            "url",
            "title",
            "path",
            "page_id",
            "offset",
            "next_offset",
            "total_chars",
            "content_type",
            "encoding",
            "status_code",
            "format",
            "extraction_truncated",
            "content_truncated",
            "error",
        )
        compact = {
            key: result[key]
            for key in keys
            if key in result
            and isinstance(result[key], (str, int, float, bool, type(None)))
        }
        compact.update(
            truncated=bool(result.get("truncated")), model_context_truncated=True
        )
        # Tool schemas bound these values, but keep malformed large metadata
        # from exceeding the final model budget as well.
        compact = {
            key: value[:2048] if isinstance(value, str) else value
            for key, value in compact.items()
        }
        content = result["content"]
        # Reserve enough room for both flags and a continuation offset before
        # choosing the exact text prefix (JSON escaping counts toward budget).
        compact["truncated"] = True
        compact["content_truncated"] = True
        compact["content"] = _fit_text(compact, "content", content, limit)
        clipped = len(compact["content"]) < len(content)
        if clipped and isinstance(result.get("offset"), int) and result.get("page_id"):
            compact["next_offset"] = result["offset"] + len(compact["content"])
            compact["content"] = _fit_text(
                compact, "content", compact["content"], limit
            )
            compact["next_offset"] = result["offset"] + len(compact["content"])
        if not clipped:
            compact["truncated"] = bool(result.get("truncated"))
            compact["content_truncated"] = bool(result.get("content_truncated"))
        if _size(compact) <= limit:
            return compact
    compact = {"truncated": True, "model_context_truncated": True}
    encoded = json.dumps(result, ensure_ascii=False)
    compact["preview"] = _fit_text(compact, "preview", encoded, limit)
    return compact
