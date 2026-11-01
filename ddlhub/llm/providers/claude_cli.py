"""本机 claude 命令（headless）。零配置、零额外成本，作为兜底默认。"""

from __future__ import annotations

import re
import shutil
import subprocess
import time

from ... import net
from .base import Provider, ProviderError

# stderr 里出现这些，基本可以判定是上游抖动而不是内容错误，值得重试
_NET_HINTS = re.compile(
    r"(?i)rate.?limit|429|timeout|timed out|network|connection|socket|econn|50[0234]"
)


class ClaudeCLIProvider(Provider):
    name = "claude-cli"

    def __init__(self, model: str = "", retry: dict | None = None):
        self.model = model
        self.retry = net.normalize(retry)
        if not shutil.which("claude"):
            raise ProviderError(
                "本机找不到 claude 命令。要么装 Claude Code CLI，"
                "要么在 config.yaml 里换一个 provider（如 openrouter）"
            )

    def complete(self, prompt: str, timeout: int = 240) -> str:
        attempts = self.retry["max_attempts"] if self.retry["enabled"] else 1
        last = ""
        for attempt in range(1, attempts + 1):
            try:
                proc = subprocess.run(
                    ["claude", "-p", "--output-format", "text"],
                    input=prompt,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                    shell=False,
                )
            except subprocess.TimeoutExpired:
                # 时间预算已经花完，再重试只会翻倍等待
                raise ProviderError(f"claude cli 超时（{timeout}s）", transient=True) from None
            except OSError as e:
                raise ProviderError(f"claude cli 启动失败：{e}", transient=True) from None

            if proc.returncode == 0:
                return proc.stdout

            last = (proc.stderr or "")[:500] or f"退出码 {proc.returncode}"
            transient = bool(_NET_HINTS.search(last))
            if attempt >= attempts or not transient:
                raise ProviderError(
                    f"claude cli 退出码 {proc.returncode}: {last}", transient=transient
                )
            wait = net.delay_for(attempt, self.retry)
            print(
                f"[retry] llm:claude-cli 第 {attempt}/{attempts} 次失败"
                f"（{net.redact(last)[:120]}），{wait:.1f}s 后重试",
                flush=True,
            )
            time.sleep(wait)

        raise ProviderError(f"claude cli 失败：{last}")
