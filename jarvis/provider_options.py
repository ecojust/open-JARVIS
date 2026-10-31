"""Small provider-specific Chat Completions options, without SDK imports."""

from typing import Any
from urllib.parse import urlsplit


def chat_completion_body_options(base_url: str) -> dict[str, Any]:
    """Use DeepSeek's faster non-thinking tool protocol on its official host.

    Thinking is enabled by default there and requires replaying provider-specific
    reasoning_content, which the current MAF and native histories do not retain.
    Proxy endpoints keep their own defaults, even when using a DeepSeek model.
    """
    try:
        endpoint = urlsplit(base_url)
        if (
            endpoint.scheme in {"http", "https"}
            and endpoint.hostname == "api.deepseek.com"
        ):
            return {"thinking": {"type": "disabled"}}
    except ValueError:
        pass
    return {}
