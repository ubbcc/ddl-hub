"""Anthropic Messages API（原来的 backend: api）。"""

from __future__ import annotations

import os

import requests

from .base import Provider, ProviderError

DEFAULT_BASE = "https://api.anthropic.com"
DEFAULT_MODEL = "claude-sonnet-5-5"


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(
        self,
        api_key: str = "",
        model: str = "",
        base_url: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not self.api_key:
            raise ProviderError("没有 ANTHROPIC_API_KEY（或 config.yaml 里的 llm.api_key）")
        self.base_url = (base_url or DEFAULT_BASE).rstrip("/")
        self.model = model or DEFAULT_MODEL
        self.max_tokens = max_tokens
        self.temperature = temperature

    def complete(self, prompt: str, timeout: int = 240) -> str:
        try:
            resp = requests.post(
                f"{self.base_url}/v1/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self.model,
                    "max_tokens": self.max_tokens,
                    "temperature": self.temperature,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=timeout,
            )
        except requests.RequestException as e:
            raise ProviderError(f"连不上 Anthropic：{e}") from None
        if resp.status_code >= 400:
            raise ProviderError(f"Anthropic 返回 {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        return "".join(b.get("text", "") for b in data.get("content", []))
