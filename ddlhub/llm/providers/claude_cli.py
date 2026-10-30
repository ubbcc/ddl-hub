"""本机 claude 命令（headless）。零配置、零额外成本，作为兜底默认。"""

from __future__ import annotations

import shutil
import subprocess

from .base import Provider, ProviderError


class ClaudeCLIProvider(Provider):
    name = "claude-cli"

    def __init__(self, model: str = ""):
        self.model = model
        if not shutil.which("claude"):
            raise ProviderError(
                "本机找不到 claude 命令。要么装 Claude Code CLI，"
                "要么在 config.yaml 里换一个 provider（如 openrouter）"
            )

    def complete(self, prompt: str, timeout: int = 240) -> str:
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
        if proc.returncode != 0:
            raise ProviderError(f"claude cli 退出码 {proc.returncode}: {(proc.stderr or '')[:500]}")
        return proc.stdout
