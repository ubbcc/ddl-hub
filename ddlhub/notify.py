"""Telegram 提醒：每日摘要 + 临期推送。复用本机已配置的机器人 token。"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

import requests

from . import db, kinds, net

DEFAULT_CHAT_ID = "8058536155"
TOKEN_ENV_FILE = Path.home() / ".claude" / "channels" / "telegram" / ".env"


def _token(cfg: dict | None = None) -> str:
    tok = os.environ.get("DDLHUB_TG_TOKEN", "")
    if tok:
        return tok
    tg = ((cfg or {}).get("notify") or {}).get("telegram") or {}
    if tg.get("token"):
        return tg["token"]
    if TOKEN_ENV_FILE.exists():
        for line in TOKEN_ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                return line.split("=", 1)[1].strip()
    return ""


def _chat_id(cfg: dict | None = None) -> str:
    tg = ((cfg or {}).get("notify") or {}).get("telegram") or {}
    return str(tg.get("chat_id") or os.environ.get("DDLHUB_TG_CHAT") or DEFAULT_CHAT_ID)


def _tg_retry_after(resp: requests.Response) -> float | None:
    """Telegram 的限流等待放在 JSON body 的 parameters 里，不在响应头。"""
    if resp.status_code != 429:
        return None
    try:
        params = (resp.json() or {}).get("parameters") or {}
        return float(params.get("retry_after"))
    except (ValueError, TypeError, AttributeError):
        return None


def send(text: str, cfg: dict | None = None, chat_id: str | None = None) -> bool:
    token = _token(cfg)
    if not token:
        raise RuntimeError("没有 Telegram token：设置 config.yaml 的 notify.telegram.token 或环境变量 DDLHUB_TG_TOKEN")
    try:
        resp = net.request(
            "POST",
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id or _chat_id(cfg),
                "text": text[:4000],
                "disable_web_page_preview": True,
            },
            timeout=30,
            retry=net.settings(cfg, "notify"),
            idempotent=False,
            # 推送宁可偶尔重复一条，也别因为一次抖动丢掉当天的摘要
            retry_timeouts=True,
            label="telegram:sendMessage",
            retry_after_of=_tg_retry_after,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(f"Telegram 推送失败：{net.redact(e)}") from None
    return bool(resp.json().get("ok"))


def _fmt_due(due: datetime) -> str:
    now = datetime.now(db.TZ)
    delta = due - now
    mins = int(delta.total_seconds() // 60)
    if mins < 0:
        d = -mins
        if d < 60:
            return f"逾期 {d} 分钟"
        if d < 1440:
            return f"逾期 {d // 60} 小时"
        return f"逾期 {d // 1440} 天"
    if mins < 60:
        return f"{mins} 分钟后"
    if mins < 1440:
        return f"{mins // 60} 小时后"
    return f"{mins // 1440} 天后（{due:%m-%d %H:%M}）"


def digest(conn=None, cfg: dict | None = None, hours: int = 72) -> str:
    own = conn is None
    conn = conn or db.connect()
    try:
        tasks = db.list_tasks(conn, "open")
    finally:
        if own:
            conn.close()

    now = datetime.now(db.TZ)
    buckets: dict[str, list[tuple[datetime, dict]]] = {
        "overdue": [], "today": [], "soon": [], "later": [], "undated": [],
    }
    for t in tasks:
        if not t.get("due_at"):
            buckets["undated"].append((now, t))
            continue
        due = datetime.fromisoformat(t["due_at"]).astimezone(db.TZ)
        h = (due - now).total_seconds() / 3600
        if h < 0:
            buckets["overdue"].append((due, t))
        elif h <= 24:
            buckets["today"].append((due, t))
        elif h <= hours:
            buckets["soon"].append((due, t))
        else:
            buckets["later"].append((due, t))

    label = {
        "overdue": "⚠️ 已逾期",
        "today": "🔥 24 小时内",
        "soon": "⏳ 三天内" if hours == 72 else f"⏳ {hours} 小时内",
        "later": "📅 更远",
        "undated": "❓ 待确认时间",
    }

    lines = [f"📋 作业 DDL 汇总  {now:%m-%d %H:%M}"]
    total = 0
    for key in ("overdue", "today", "soon", "later", "undated"):
        items = buckets[key]
        if not items:
            continue
        lines.append("")
        lines.append(f"{label[key]}（{len(items)}）")
        for due, t in items[:12]:
            emoji = kinds.EMOJI.get(kinds.normalize(t.get("kind")) or "", "")
            head = f"• {emoji + ' ' if emoji else ''}{t['title']}"
            if t.get("course"):
                head += f" · {t['course']}"
            tail = f"  〔{_fmt_due(due)}〕" if t.get("due_at") else f"  〔{t.get('due_text') or '无日期'}〕"
            lines.append(head + tail)
            total += 1
        if len(items) > 12:
            lines.append(f"  …还有 {len(items) - 12} 条")

    if total == 0:
        lines.append("")
        lines.append("暂时没有待办作业 🎉")
        return "\n".join(lines)

    # 哪门课最压人：按未完成条数排，只列前三
    by_course: dict[str, int] = {}
    for t in tasks:
        name = t.get("course")
        if name:
            by_course[name] = by_course.get(name, 0) + 1
    if len(by_course) >= 2:
        top = sorted(by_course.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
        lines.append("")
        lines.append("📚 最压人：" + " · ".join(f"{n} {c}" for n, c in top))
    return "\n".join(lines)
