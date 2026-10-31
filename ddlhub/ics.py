"""生成 .ics 日历订阅，手机日历 / Outlook 可直接订阅。"""

from __future__ import annotations

from datetime import datetime, timedelta

from . import db, kinds

KIND_EMOJI = kinds.EMOJI


def _fold(line: str, limit: int = 74) -> str:
    out, cur, cur_len = [], [], 0
    for ch in line:
        n = len(ch.encode("utf-8"))
        if cur_len + n > limit:
            out.append("".join(cur))
            cur, cur_len = [], 0
        cur.append(ch)
        cur_len += n
    if cur:
        out.append("".join(cur))
    return "\r\n ".join(out)


def _esc(s: str) -> str:
    return (
        (s or "")
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%S")


def build_ics(tasks: list[dict], calendar_name: str = "作业 DDL") -> str:
    now = datetime.now(db.TZ)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//ddl-hub//CN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_esc(calendar_name)}",
        "X-WR-TIMEZONE:Asia/Shanghai",
    ]
    for t in tasks:
        if not t.get("due_at"):
            continue
        try:
            due = datetime.fromisoformat(t["due_at"]).astimezone(db.TZ)
        except ValueError:
            continue
        # UID 用稳定的数据库 id：指纹会随改名/改课变化，一变日历就当成新事件
        uid = f"ddlhub-task-{t['id']}@local"
        emoji = KIND_EMOJI.get(t.get("kind") or "", "⏰")
        summary = f"{emoji} {t['title']}" + (f" - {t['course']}" if t.get("course") else "")
        desc = "\n".join(
            x
            for x in [
                f"课程：{t.get('course') or '未知'}",
                f"类型：{t.get('kind') or ''}",
                f"原话：{t.get('due_text') or ''}",
                f"来源：{t.get('source') or ''} {t.get('source_ref') or ''}",
            ]
            if x.strip()
        )
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}@ddl-hub",
            f"DTSTAMP:{_fmt(now)}Z",
            f"DTSTART:{_fmt(due)}",
            f"DTEND:{_fmt(due + timedelta(minutes=30))}",
            f"SUMMARY:{_esc(summary)}",
            f"DESCRIPTION:{_esc(desc)}",
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            "DESCRIPTION:作业快到期了",
            "TRIGGER:-P1D",
            "END:VALARM",
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            "DESCRIPTION:作业 3 小时后截止",
            "TRIGGER:-PT3H",
            "END:VALARM",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(l) if len(l.encode()) > 74 else l for l in lines) + "\r\n"
