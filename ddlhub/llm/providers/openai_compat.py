"""OpenAI 兼容的 /chat/completions。

一家实现覆盖大多数服务商：DeepSeek、智谱 GLM、硅基流动、月之暗面、通义，
以及本地的 Ollama / vLLM / LM Studio。OpenRouter 也走这儿（见 openrouter.py）。
"""

from __future__ import annotations

import requests

from .base import Provider, ProviderError


def _content_of(message: dict) -> str:
    """兼容 content 是字符串、或 [{type:text,text:...}] 这两种返回。"""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") in (None, "text"):
                parts.append(str(p.get("text", "")))
            elif isinstance(p, str):
                parts.append(p)
        return "".join(parts)
    return ""


class OpenAICompatProvider(Provider):
    name = "openai-compat"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        extra_headers: dict | None = None,
        extra_body: dict | None = None,
    ):
        if not model:
            raise ProviderError("没有指定模型（config.yaml 里的 llm.model）")
        self.base_url = (base_url or "").rstrip("/")
        if not self.base_url:
            raise ProviderError("openai-compat 需要 base_url，例如 https://api.deepseek.com/v1")
        self.api_key = api_key or ""
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.extra_headers = dict(extra_headers or {})
        self.extra_body = dict(extra_body or {})

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        h.update(self.extra_headers)
        return h

    def _payload(self, prompt: str) -> dict:
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        body.update(self.extra_body)
        return body

    def complete(self, prompt: str, timeout: int = 240) -> str:
        url = f"{self.base_url}/chat/completions"
        try:
            resp = requests.post(url, headers=self._headers(), json=self._payload(prompt), timeout=timeout)
        except requests.RequestException as e:
            raise ProviderError(f"连不上 {self.describe()}：{e}") from None
        if resp.status_code >= 400:
            raise ProviderError(f"{self.describe()} 返回 {resp.status_code}: {resp.text[:300]}")
        try:
            data = resp.json()
            return _content_of(data["choices"][0]["message"])
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise ProviderError(f"{self.describe()} 返回格式不对：{resp.text[:300]}") from e
