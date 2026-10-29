"""用 LLM 从聊天消息里抽作业截止时间。"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime

WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

SYSTEM_PROMPT = """你是作业截止时间抽取器。今天是 {today}（{weekday}），时区 Asia/Shanghai (+08:00)。

下面是一批群聊/通知消息，每条一行，格式：
[序号] 来源|群名|发送时间|发送者: 内容

从严抽取「有明确交付时限」的事项：作业、实验报告、课程论文、考试、测验、答辩、展示、报名、投票。
不抽：闲聊、广告、已完成的确认、纯提问、没有时限的普通通知。

硬规则：
1. due_at 用 ISO 8601 带偏移，如 2026-11-03T23:59:00+08:00。只有日期没有钟点的，填该日 23:59。
2. "下周三""明天""周五前""本周日"等相对时间，一律以【该条消息自己的发送时间】为基准推算，不是以今天为基准。
3. 原文没写年份时，取不早于该消息发送日期的最近年份（跨年时自动到下一年）。
4. "截止/DDL/deadline/之前/前交/提交/按时交"视为时限；"上课""打卡""签到"不是作业。
5. 确实无法确定日期的，due_at 填 null，due_text 保留原话，confidence 不高于 0.4。
6. title 必须能区分同类作业，保留"第几次/第几章/实验几/小组"等编号，不超过 30 字。
   title 里不要写截止时间，也不要写"延期""改为"等状态词——改期信息只体现在 due_at 上。
7. course 填课程名；判不出就 null。kind 只能是 作业/考试/实验/论文/展示/其他。
8. 消息可能是脏数据：带表情、CQ 码、HTML 残留、乱码、错别字、语音转文字错误、半句话。
   按语义尽力理解，别因为格式脏就放弃；实在拿不准就把 confidence 调低，仍然输出。
9. 若消息里截止时间后面带了「（学期末）」「（长期限）」这类短标注，说明这个时间很可能是
   系统默认值而不是老师设的。这种情况仍然要输出这条作业，但要把这 3-4 字标注原样放在
   标题最前面，格式如「学期末 第十二章积极自我」，并把 confidence 压到 0.5 以下。

只输出 JSON 数组，不要解释，不要 markdown 代码块。每个元素字段：
{{"idx": 序号, "title": "", "course": null, "kind": "作业", "due_at": null, "due_text": "原话片段", "confidence": 0.0, "evidence": "触发该事项的原文，<=60字"}}

没有符合条件的事项就输出 []。
"""


def _fmt_msg(m: dict, idx: int) -> str:
    content = (m.get("content") or "").replace("\n", " ⏎ ").strip()
    if len(content) > 600:
        content = content[:600] + "…"
    return (
        f"[{idx}] {m.get('source', '')}|{m.get('chat', '')}|"
        f"{m.get('sent_at') or ''}|{m.get('sender') or ''}: {content}"
    )


def _extract_json_array(text: str) -> list:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    try:
        v = json.loads(text)
        return v if isinstance(v, list) else []
    except json.JSONDecodeError:
        pass
    depth, start = 0, None
    for i, ch in enumerate(text):
        if ch == "[":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    v = json.loads(text[start : i + 1])
                    return v if isinstance(v, list) else []
                except json.JSONDecodeError:
                    start = None
    return []


def _call_claude_cli(prompt: str, timeout: int) -> str:
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
        raise RuntimeError(f"claude cli 退出码 {proc.returncode}: {(proc.stderr or '')[:500]}")
    return proc.stdout


def _call_anthropic_api(prompt: str, timeout: int) -> str:
    import os

    import requests

    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("未设置 ANTHROPIC_API_KEY")
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": os.environ.get("DDLHUB_MODEL", "claude-sonnet-5-5"),
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    return "".join(b.get("text", "") for b in data.get("content", []))


def extract_batch(messages: list[dict], backend: str = "claude-cli", timeout: int = 240) -> list[dict]:
    if not messages:
        return []
    today = datetime.now().strftime("%Y-%m-%d")
    weekday = WEEKDAYS[datetime.now().weekday()]
    body = "\n".join(_fmt_msg(m, i) for i, m in enumerate(messages))
    prompt = SYSTEM_PROMPT.format(today=today, weekday=weekday) + "\n消息：\n" + body

    raw = _call_anthropic_api(prompt, timeout) if backend == "api" else _call_claude_cli(prompt, timeout)
    items = _extract_json_array(raw)

    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        idx = it.get("idx")
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            continue
        if not (0 <= idx < len(messages)):
            continue
        title = (it.get("title") or "").strip()
        if not title:
            continue
        src = messages[idx]
        out.append(
            {
                "title": title[:120],
                "course": (it.get("course") or None),
                "kind": it.get("kind") or "其他",
                "due_at": _clean_due(it.get("due_at")),
                "due_text": (it.get("due_text") or "")[:200],
                "confidence": _clamp(it.get("confidence")),
                "evidence": (it.get("evidence") or "")[:200],
                "source": src.get("source"),
                "source_ref": f"{src.get('chat', '')}#{src.get('id', '')}",
            }
        )
    return out


def _clamp(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(1.0, f))


def _clean_due(v) -> str | None:
    if not v or not isinstance(v, str):
        return None
    s = v.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    # 脏数据里常见的离谱年份（1970、2099）直接丢掉，宁可让用户手工补
    if not (datetime.now().year - 2 <= dt.year <= datetime.now().year + 3):
        return None
    return dt.isoformat(timespec="seconds")
