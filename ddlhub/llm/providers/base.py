"""提供商抽象：统一成 complete(prompt) -> str。"""

from __future__ import annotations


class ProviderError(RuntimeError):
    """配置缺失、网络失败、上游报错，统一用这个抛，调用方按失败处理。

    transient=True 表示重试耗尽的网络/限流类故障：调用方不要再拆批重试，
    把整批留到下次运行即可。
    """

    transient = False

    def __init__(self, message: str = "", *, transient: bool = False):
        super().__init__(message)
        self.transient = bool(transient)


class Provider:
    name = "provider"
    model = ""

    def complete(self, prompt: str, timeout: int = 240) -> str:  # pragma: no cover - 抽象
        raise NotImplementedError

    def describe(self) -> str:
        return f"{self.name}/{self.model}" if self.model else self.name

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.describe()}>"
