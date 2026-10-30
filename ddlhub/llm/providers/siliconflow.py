"""硅基流动 SiliconFlow：OpenAI 兼容，国内直连。

注册送额度，Qwen / GLM / DeepSeek 都有；模型 id 带组织前缀，如
`deepseek-ai/DeepSeek-V3`、`Qwen/Qwen3-8B`、`THUDM/glm-4-9b-chat`。
具体可用的 id 以官方控制台为准（这里的默认值只是起点）。
"""

from __future__ import annotations

from .base import ProviderError
from .openai_compat import OpenAICompatProvider

DEFAULT_BASE = "https://api.siliconflow.cn/v1"
DEFAULT_MODEL = "deepseek-ai/DeepSeek-V3"


class SiliconFlowProvider(OpenAICompatProvider):
    name = "siliconflow"

    def __init__(
        self,
        api_key: str,
        model: str = "",
        base_url: str = "",
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ):
        if not api_key:
            raise ProviderError(
                "没有硅基流动 API key。在 config.yaml 里填 llm.api_key，"
                "或设置环境变量 SILICONFLOW_API_KEY（在 cloud.siliconflow.cn 申请）"
            )
        super().__init__(
            base_url=base_url or DEFAULT_BASE,
            api_key=api_key,
            model=model or DEFAULT_MODEL,
            temperature=temperature,
            max_tokens=max_tokens,
        )
