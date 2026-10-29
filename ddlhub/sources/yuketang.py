"""雨课堂：用登录后的 Cookie 拉课程 → 课程活动（作业 / 小测）→ 截止时间入库。

接口（实测）：
  GET {base}/v2/api/web/logs/learn/{classroom_id}?actype=-1&page=0&offset=100&sort=-1
返回 data.activities[]，每条含 classroom_id / title / type / id / create_time(ms)，
作业与小测的截止时间在 content.score_d（毫秒时间戳），data.has_more 控制翻页。

type 19 = 作业，20 = 小测；其他 type 若标题带作业类关键词也收。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import requests

from .. import db

DEBUG_PATH = Path(__file__).resolve().parents[2] / "data" / "yt_debug.json"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

ACTIVITY_URL = "/v2/api/web/logs/learn/{rid}?actype=-1&page={page}&offset=100&sort=-1"
PAGE_LIMIT = 10
LOOKBACK_DAYS = 7
# 截止时间比创建时间晚这么多天，基本可以断定是系统默认值而不是老师设的
SUSPECT_SPAN_DAYS = 60
# 学期末默认截止日大致落在这两个区间
SEMESTER_END_RANGES = (((1, 15), (2, 15)), ((7, 10), (8, 20)))

TYPE_LABELS = {"19": "作业", "20": "小测"}
TITLE_HINTS = ("作业", "小测", "测验", "考试", "实验", "报告", "提交")
TITLE_KEYS = ("title", "name", "notice_title", "subject", "course_name")
DEADLINE_KEYS = (
    "score_d", "deadline", "end_time", "endtime", "due_time", "duetime",
    "expire_time", "expiretime", "close_time", "closetime", "submit_deadline",
)


def _cookie_map(raw: str) -> dict:
    out = {}
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _pick(d: dict, keys) -> str | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, (str, int)) and str(v).strip():
            return str(v).strip()
    return None


def _to_iso(v) -> str | None:
    """秒/毫秒时间戳或日期字符串 → +08:00 ISO。"""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)) and v > 0:
        ts = float(v)
        if ts > 1e11:  # 毫秒
            ts /= 1000
        try:
            return datetime.fromtimestamp(ts, db.TZ).isoformat(timespec="seconds")
        except (OSError, ValueError):
            return None

    s = str(v).strip()
    if re.fullmatch(r"\d{10}(\d{3})?", s):
        return _to_iso(int(s))

    if "T" in s or re.search(r"[+-]\d{2}:?\d{2}$", s):
        try:
            dt = datetime.fromisoformat(s.replace("/", "-").replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=db.TZ)
            return dt.isoformat(timespec="seconds")
        except ValueError:
            pass

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s[: len(fmt) + 2], fmt).replace(tzinfo=db.TZ).isoformat(timespec="seconds")
        except ValueError:
            continue

    try:
        return datetime.strptime(s[:10].replace("/", "-"), "%Y-%m-%d").replace(tzinfo=db.TZ).isoformat(timespec="seconds")
    except ValueError:
        return None


def _human(iso: str) -> str:
    """2026-11-05T23:59:00+08:00 → 2026-11-05 23:59"""
    return iso[:16].replace("T", " ") if "T" in iso else iso[:10]


def _get_json(sess: requests.Session, url: str, headers: dict | None = None):
    r = sess.get(url, timeout=30, headers=headers or {})
    if r.status_code in (401, 403):
        raise RuntimeError("雨课堂认证失败（cookie 过期或缺 sessionid），重新复制一次 Cookie")
    if r.status_code == 404:
        return None
    r.raise_for_status()
    try:
        return r.json()
    except ValueError:
        return None


def _courses(sess, base) -> list[dict]:
    data = _get_json(sess, f"{base}/v2/api/web/courses/list?identity=2")
    if not data:
        _dump_debug({"stage": "courses", "note": "courses/list 没有返回 JSON"})
        raise RuntimeError("雨课堂课程列表拉取失败，cookie 可能无效（已写 data/yt_debug.json）")

    found, seen = [], set()
    for d in _walk(data):
        # 实际返回里课程号嵌在 course.id，课堂号在顶层 classroom_id
        nested = d.get("course") if isinstance(d.get("course"), dict) else {}
        cid = d.get("course_id") or d.get("cid") or nested.get("id")
        rid = d.get("classroom_id") or d.get("classroomId")
        if not cid or not rid:
            continue
        key = (str(cid), str(rid))
        if key in seen:
            continue
        seen.add(key)
        found.append(
            {
                "cid": str(cid),
                "rid": str(rid),
                "name": _pick(nested, ("name", "short_name"))
                or _pick(d, ("course_name", "name", "title"))
                or f"课程{rid}",
            }
        )
    return found


def _activities(sess, base, course: dict) -> list[dict]:
    """按 has_more 翻页拉课程活动，返回原始 activity 节点。"""
    out, seen = [], set()
    for page in range(PAGE_LIMIT):
        data = _get_json(
            sess,
            base + ACTIVITY_URL.format(rid=course["rid"], page=page),
            headers={"classroom-id": course["rid"]},
        )
        body = (data or {}).get("data") or {}
        acts = body.get("activities") if isinstance(body, dict) else None
        if not isinstance(acts, list) or not acts:
            if page == 0 and data is not None:
                _dump_debug({"stage": "activities", "course": course["name"], "raw": data})
            break
        fresh = 0
        for act in acts:
            if not isinstance(act, dict):
                continue
            aid = act.get("id")
            if aid in seen:
                continue
            seen.add(aid)
            fresh += 1
            out.append(act)
        if fresh == 0 or not body.get("has_more"):
            break
    return out


def _deadline(act: dict) -> str | None:
    content = act.get("content") if isinstance(act.get("content"), dict) else {}
    for key in DEADLINE_KEYS:
        for src in (content, act):
            raw = src.get(key)
            if raw in (None, "", 0):
                continue
            iso = _to_iso(raw)
            if iso:
                return iso
    return None


def _label(act: dict) -> str | None:
    label = TYPE_LABELS.get(str(act.get("type")))
    if label:
        return label
    title = str(act.get("title") or "")
    return next((w for w in TITLE_HINTS if w in title), None)


def _is_semester_end(due_dt: datetime) -> bool:
    md = (due_dt.month, due_dt.day)
    return any(lo <= md <= hi for lo, hi in SEMESTER_END_RANGES)


def _suspect_label(due_dt: datetime, created_iso: str | None) -> str:
    """这个截止时间可疑时，返回一个不超过 4 字的理由；不可疑返回空串。

    教师没单独设截止时间时，雨课堂会把一批作业统一落到学期末（例如全是
    2027-01-31 23:59）。这种日期要保留，但标出来免得当成真实 DDL 去赶。
    """
    span = 0
    if created_iso:
        span = (due_dt - datetime.fromisoformat(created_iso)).days
    if span > SUSPECT_SPAN_DAYS or _is_semester_end(due_dt):
        return "学期末" if _is_semester_end(due_dt) else "长期限"
    return ""


def _entry(act: dict, course: dict) -> dict | None:
    title = str(act.get("title") or "").strip()
    if not title:
        return None
    label = _label(act)
    if label is None:
        return None
    due = _deadline(act)
    if not due:
        return None
    due_dt = datetime.fromisoformat(due)
    if due_dt < datetime.now(db.TZ) - timedelta(days=LOOKBACK_DAYS):
        return None

    created = _to_iso(act.get("create_time"))
    suspect = _suspect_label(due_dt, created)

    created_txt = f"，创建于 {_human(created)}" if created else ""
    flag_txt = f"（{suspect}）" if suspect else ""
    text = (
        f"【雨课堂】课程《{course['name']}》{label}「{title}」，"
        f"截止时间 {_human(due)}{flag_txt}{created_txt}"
    )
    return {
        "source": "yuketang",
        "chat": course["name"],
        "sender": "雨课堂",
        "sent_at": created or db.now_iso()[:16],
        "content": text,
        # 截止时间进 uid：改期会算作新消息，从而触发改期合并
        "msg_uid": f"ykt:{course['cid']}:{act.get('id')}:{due}",
    }


def collect(conn, cfg, sec) -> dict:
    host = (sec.get("host") or "www.yuketang.cn").strip()
    host = re.sub(r"^https?://", "", host).strip("/")
    cookie = (sec.get("cookie") or "").strip()
    if not cookie or cookie.startswith("<"):
        raise RuntimeError("还没填雨课堂 Cookie：config.yaml → sources.yuketang.cookie")

    base = f"https://{host}"
    sess = requests.Session()
    sess.headers.update(
        {
            "Cookie": cookie,
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Referer": f"{base}/",
            "xtbz": "ykt",
        }
    )
    csrf = _cookie_map(cookie).get("csrftoken")
    if csrf:
        sess.headers["X-CSRFToken"] = csrf

    courses = _courses(sess, base)
    if not courses:
        raise RuntimeError("没解析到课程列表，cookie 可能无效或字段变了")

    items, scanned = [], 0
    for course in courses:
        try:
            acts = _activities(sess, base, course)
        except Exception as e:
            print(f"[yuketang] 跳过课程 {course['name']}：{e}")
            continue
        scanned += len(acts)
        for act in acts:
            entry = _entry(act, course)
            if entry:
                items.append(entry)

    added = db.add_messages(conn, items)
    return {"added": added, "courses": len(courses), "activities": scanned, "due_items": len(items)}


def _dump_debug(payload) -> None:
    try:
        DEBUG_PATH.parent.mkdir(parents=True, exist_ok=True)
        DEBUG_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2)[:200_000], encoding="utf-8"
        )
    except OSError:
        pass
