"""提供商注册表：把 config.yaml 的 llm 段变成 Provider 实例。"""

from __future__ import annotations

import os

from .anthropic import AnthropicProvider
from .base import Provider, ProviderError
from .claude_cli import ClaudeCLIProvider
from .openai_compat import OpenAICompatProvider
from .openrouter import OpenRouterProvider
from .siliconflow import SiliconFlowProvider

__all__ = [
    "Provider",
    "ProviderError",
    "ClaudeCLIProvider",
    "AnthropicProvider",
    "OpenAICompatProvider",
    "OpenRouterProvider",
    "SiliconFlowProvider",
    "build",
    "normalize_name",
    "PRESETS",
]

# 旧的 backend 取值继续认，避免老配置直接报错
ALIASES = {
    "cli": "claude-cli",
    "claude": "claude-cli",
    "api": "anthropic",
    "openai": "openai-compat",
    "compat": "openai-compat",
    "glm": "openai-compat",
    "zhipu": "openai-compat",
    "ollama": "openai-compat",
    "silicon": "siliconflow",
    "sf": "siliconflow",
}

PRESETS = {
    "siliconflow": {
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "deepseek-ai/DeepSeek-V3",
        "key_env": "SILICONFLOW_API_KEY",
        "label": "硅基流动 SiliconFlow（默认，国内直连）",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "model": "deepseek/deepseek-v4-flash",
        "key_env": "OPENROUTER_API_KEY",
        "label": "OpenRouter（deepseek-v4-flash 非深度思考）",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "key_env": "DEEPSEEK_API_KEY",
        "label": "DeepSeek 官方",
    },
    "anthropic": {
        "base_url": "",
        "model": "claude-sonnet-5-5",
        "key_env": "ANTHROPIC_API_KEY",
        "label": "Anthropic 官方",
    },
    "claude-cli": {
        "base_url": "",
        "model": "",
        "key_env": "",
        "label": "本机 claude 命令（零配置）",
    },
    "openai-compat": {
        "base_url": "",
        "model": "",
        "key_env": "",
        "label": "其他 OpenAI 兼容服务（自填 base_url）",
    },
}


def normalize_name(name: str) -> str:
    n = (name or "").strip().lower()
    return ALIASES.get(n, n)


def build(cfg: dict | None = None) -> Provider:
    """按 llm 配置造一个 provider。配置不全就抛 ProviderError，信息说清楚缺什么。"""
    c = dict(cfg or {})
    raw = str(c.get("provider") or c.get("backend") or "claude-cli")
    name = normalize_name(raw)
    preset = PRESETS.get(name, {})

    model = str(c.get("model") or "") or os.environ.get("DDLHUB_MODEL", "") or preset.get("model", "")
    base_url = str(c.get("base_url") or "") or preset.get("base_url", "")
    api_key = (
        str(c.get("api_key") or "")
        or os.environ.get("DDLHUB_API_KEY", "")
        or os.environ.get(preset.get("key_env") or "", "")
    )
    temperature = float(c.get("temperature", 0) or 0)
    max_tokens = int(c.get("max_tokens", 4096) or 4096)

    if name == "claude-cli":
        return ClaudeCLIProvider()

    if name == "anthropic":
        return AnthropicProvider(
            api_key=api_key,
            model=model,
            base_url=base_url,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    if name == "openrouter":
        return OpenRouterProvider(
            api_key=api_key,
            model=model,
            base_url=base_url,
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning=bool(c.get("reasoning", False)),
        )

    if name == "siliconflow":
        return SiliconFlowProvider(
            api_key=api_key,
            model=model,
            base_url=base_url,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    if name in ("openai-compat", "deepseek"):
        if not base_url:
            raise ProviderError(
                f"provider={raw} 需要 base_url，例如 https://api.deepseek.com/v1"
            )
        if not api_key and "localhost" not in base_url and "127.0.0.1" not in base_url:
            raise ProviderError(f"provider={raw} 需要 api_key（配置里填，或设置对应环境变量）")
        return OpenAICompatProvider(
            base_url=base_url,
            api_key=api_key,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    raise ProviderError(
        f"不认识的 provider：{raw}。可用：claude-cli / siliconflow / openrouter / deepseek / anthropic / openai-compat"
    )
