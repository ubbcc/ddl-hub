"""OpenAI 兼容的 /chat/completions。

一家实现覆盖大多数服务商：DeepSeek、智谱 GLM、硅基流动、月之暗面、通义，
以及本地的 Ollama / vLLM / LM Studio。OpenRouter 也走这儿（见 openrouter.py）。
"""

from __future__ import annotations

import requests

from ... import net
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
        retry: dict | None = None,
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
        self.retry = net.normalize(retry)

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

    def _post(self, payload: dict, timeout: int) -> str:
        url = f"{self.base_url}/chat/completions"
        try:
            resp = net.request(
                "POST",
                url,
                headers=self._headers(),
                json=payload,
                timeout=timeout,
                retry=self.retry,
                idempotent=False,
                label=f"llm:{self.name}",
            )
        except requests.RequestException as e:
            raise ProviderError(f"连不上 {self.describe()}：{net.redact(e)}", transient=True) from None
        if resp.status_code >= 400:
            raise ProviderError(
                f"{self.describe()} 返回 {resp.status_code}: {resp.text[:300]}",
                transient=resp.status_code in self.retry["retry_statuses"],
            )
        try:
            data = resp.json()
            return _content_of(data["choices"][0]["message"])
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise ProviderError(f"{self.describe()} 返回格式不对：{resp.text[:300]}") from e

    def complete(self, prompt: str, timeout: int = 240) -> str:
        return self._post(self._payload(prompt), timeout)

    def complete_image(self, prompt: str, image_path, timeout: int = 120) -> str:
        """把图片以 base64 data URL 发过去。需要模型本身支持看图（Qwen-VL / GLM-4V 等）。"""
        import base64
        import mimetypes
        from pathlib import Path as _Path

        p = _Path(image_path)
        raw = p.read_bytes()
        if len(raw) > 8 * 1024 * 1024:
            raise ProviderError(f"图片太大（{len(raw) / 1024 / 1024:.1f} MB），先压缩")
        mime = mimetypes.guess_type(p.name)[0] or "image/jpeg"
        url = "data:{};base64,{}".format(mime, base64.b64encode(raw).decode())

        payload = self._payload(prompt)
        payload["messages"] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": url}},
                ],
            }
        ]
        return self._post(payload, timeout)
