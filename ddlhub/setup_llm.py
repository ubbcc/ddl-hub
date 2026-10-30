"""LLM 配置向导：选服务商 → 填 key → 探测连通和 JSON 服从性 → 写回 config.yaml。

    python -m ddlhub.setup_llm

只改 config.yaml 里的 llm: 段，其它注释和配置原样保留；改之前先备份一份。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from . import llm
from .llm.providers import PRESETS

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"

PROBE = (
    "只输出一个 JSON 数组，不要解释，不要 markdown 代码块。"
    '数组里放一个对象：{"ok": true, "who": "ddl-hub"}'
)


def _ask(prompt: str, default: str = "") -> str:
    hint = f"（回车用默认值 {default}）" if default else ""
    raw = input(f"{prompt}{hint}: ").strip()
    return raw or default


def _probe(provider) -> tuple[bool, str]:
    """发一条小请求，确认能连通、能按 JSON 输出。"""
    started = time.time()
    try:
        raw = provider.complete(PROBE, timeout=60)
    except Exception as e:
        return False, f"调用失败：{str(e)[:300]}"
    elapsed = time.time() - started
    items = llm._extract_json_array(raw)
    if not items:
        return False, f"返回的不是 JSON 数组（{elapsed:.1f}s）：{raw[:200]!r}"
    return True, f"连上了，{elapsed:.1f}s，返回 {len(items)} 个对象"


def _patch_llm_block(text: str, block: list[str]) -> str:
    """把 config.yaml 里的 llm: 段整段换掉，其余内容（含注释）不动。"""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.rstrip() == "llm:":
            start = i
            break
    if start is None:
        body = text.rstrip("\n")
        return (body + "\n\n" if body else "") + "\n".join(block) + "\n"

    end = len(lines)
    for j in range(start + 1, len(lines)):
        s = lines[j]
        if s.strip() and not s.startswith((" ", "\t", "#")):
            end = j
            break
    tail = block + ([""] if end < len(lines) else [])
    return "\n".join(lines[:start] + tail + lines[end:]) + "\n"


def _block_for(name: str, model: str, base_url: str, api_key: str, timeout: int) -> list[str]:
    lines = ["llm:", f"  provider: {name}"]
    if model:
        lines.append(f"  model: {model}")
    if base_url:
        lines.append(f"  base_url: {base_url}")
    if api_key:
        lines.append(f"  api_key: {api_key}")
    lines += [
        f"  timeout_seconds: {timeout}",
        "  batch_size: 60",
        "  temperature: 0",
        "  max_tokens: 4096",
        "  reasoning: false     # openrouter/deepseek：关掉深度思考",
    ]
    return lines


def main() -> int:
    print("ddl-hub LLM 配置向导\n")
    choices = [(k, v["label"]) for k, v in PRESETS.items()]
    for i, (key, label) in enumerate(choices, 1):
        print(f"  {i}. {label}   [{key}]")
    pick = _ask("\n选一个（序号）", "1")
    try:
        name, _ = choices[int(pick) - 1]
    except (ValueError, IndexError):
        print("序号不对")
        return 1

    preset = PRESETS[name]
    print(f"\n配置 {preset['label']}")
    model = _ask("模型名", preset.get("model", ""))
    base_url = _ask("base_url", preset.get("base_url", ""))
    default_key = preset.get("key_env", "")
    if default_key:
        print(f"（也可以不填，改用环境变量 {default_key}）")
    api_key = ""
    if name not in ("claude-cli",):
        try:
            import getpass

            api_key = getpass.getpass("API key（不显示，留空则用环境变量）: ").strip()
        except Exception:
            api_key = input("API key（留空则用环境变量）: ").strip()
    timeout = int(_ask("超时秒数", "240") or 240)

    cfg = {
        "provider": name,
        "model": model,
        "base_url": base_url,
        "api_key": api_key,
        "timeout_seconds": timeout,
        "temperature": 0,
        "max_tokens": 4096,
        "reasoning": False,
    }

    print("\n正在探测…")
    try:
        provider = llm.build_provider(cfg)
    except llm.ProviderError as e:
        print(f"✗ 配置不完整：{e}")
        return 1

    ok, msg = _probe(provider)
    print(("✓ " if ok else "✗ ") + msg)
    if not ok:
        print("\n没有写回配置。改好再跑一次。")
        return 1

    block = _block_for(name, model, base_url, api_key, timeout)
    if CONFIG_PATH.exists():
        original = CONFIG_PATH.read_text(encoding="utf-8")
        backup = CONFIG_PATH.with_suffix(".yaml.bak")
        backup.write_text(original, encoding="utf-8")
        CONFIG_PATH.write_text(_patch_llm_block(original, block), encoding="utf-8")
        print(f"\n已写回 {CONFIG_PATH}（备份在 {backup.name}）")
    else:
        CONFIG_PATH.write_text("\n".join(block) + "\n", encoding="utf-8")
        print(f"\n已创建 {CONFIG_PATH}")

    print("\n试跑一次：python run.py --once")
    return 0


if __name__ == "__main__":
    sys.exit(main())
