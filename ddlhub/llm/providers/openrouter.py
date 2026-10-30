"""OpenRouter：本质也是 OpenAI 兼容，额外加两件事。

1. 带上 HTTP-Referer / X-Title（OpenRouter 用它做应用归因，不带也能用）
2. DeepSeek V4 Flash 这类模型默认会走「深度思考」，抽取任务不需要。
   用 `reasoning: {"enabled": false}` 显式关掉，省钱也省时延。
"""

from __future__ import annotations

from .base import ProviderError
from .openai_compat import OpenAICompatProvider

DEFAULT_BASE = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"
DEFAULT_REFERER = "https://github.com/ubbcc/ddl-hub"
DEFAULT_TITLE = "ddl-hub"


class OpenRouterProvider(OpenAICompatProvider):
    name = "openrouter"

    def __init__(
        self,
        api_key: str,
        model: str = "",
        base_url: str = "",
        temperature: float = 0.0,
        max_tokens: int = 4096,
        reasoning: bool = False,
        referer: str = DEFAULT_REFERER,
        title: str = DEFAULT_TITLE,
    ):
        if not api_key:
            raise ProviderError(
                "没有 OpenRouter API key。在 config.yaml 里填 llm.api_key，"
                "或设置环境变量 OPENROUTER_API_KEY（在 openrouter.ai/keys 申请）"
            )
        extra_body = {} if reasoning else {"reasoning": {"enabled": False}}
        super().__init__(
            base_url=base_url or DEFAULT_BASE,
            api_key=api_key,
            model=model or DEFAULT_MODEL,
            temperature=temperature,
            max_tokens=max_tokens,
            extra_headers={"HTTP-Referer": referer, "X-Title": title},
            extra_body=extra_body,
        )
