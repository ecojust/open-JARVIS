"""Small, offline configuration examples, checked against provider docs 2026-10-31.

These entries only explain the three existing settings. Choosing an entry must
not change saved configuration, make network calls, or read a user's API key.
"""


PROVIDER_HELP = (
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",
        "key_url": "https://platform.deepseek.com/api_keys",
        "note": "在 DeepSeek 开放平台创建 API Key。Flash 适合日常任务；语音识别和播报仍使用下方本地模型。",
        "docs_url": "https://api-docs.deepseek.com/",
    },
    {
        "id": "openai",
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-6.1-sol",
        "key_url": "https://platform.openai.com/api-keys",
        "note": "在 OpenAI API 平台创建项目密钥，并选用账户可访问的模型 ID；ChatGPT 订阅与 API 账户分开管理。",
        "docs_url": "https://developers.openai.com/api/docs/models/gpt-6.1-sol",
    },
    {
        "id": "bailian",
        "name": "阿里云百炼（北京）",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen3.8-max",
        "key_url": "https://bailian.console.aliyun.com/",
        "note": "此地址对应北京地域，API Key 也须在北京地域创建。使用其他地域或业务空间专属地址时，从对应控制台复制兼容接口地址。",
        "docs_url": "https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope",
    },
    {
        "id": "ollama",
        "name": "Ollama（本地服务）",
        "base_url": "http://localhost:11434/v1",
        "model": "",
        "key_url": "",
        "note": "模型名填本地已准备好、支持工具调用的模型 ID。Ollama 默认不校验 API Key，可填 ollama。其他兼容服务请使用其实际地址、模型 ID 和密钥。",
        "docs_url": "https://docs.ollama.com/api/openai-compatibility",
    },
)
