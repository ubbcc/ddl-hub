"""SQLite 存储层：消息、任务、变更历史。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "ddl.db"
TZ = timezone(timedelta(hours=8))

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  source      TEXT NOT NULL,
  chat        TEXT NOT NULL,
  sender      TEXT,
  sent_at     TEXT,
  content     TEXT NOT NULL,
  msg_uid     TEXT UNIQUE,
  ingested_at TEXT NOT NULL,
  extracted   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_msg_extracted ON messages(extracted);

CREATE TABLE IF NOT EXISTS tasks (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  fingerprint TEXT NOT NULL UNIQUE,
  title       TEXT NOT NULL,
  course      TEXT,
  kind        TEXT,
  due_at      TEXT,
  due_text    TEXT,
  status      TEXT NOT NULL DEFAULT 'open',
  confidence  REAL,
  evidence    TEXT,
  source      TEXT,
  source_ref  TEXT,
  history     TEXT NOT NULL DEFAULT '[]',
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_due ON tasks(due_at);
CREATE INDEX IF NOT EXISTS idx_task_status ON tasks(status);

CREATE TABLE IF NOT EXISTS runs (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  ok         INTEGER,
  detail     TEXT
);
"""


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path) if path else DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _norm(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"[\s\W_]+", "", t)


def _norm_title(text: str) -> str:
    """归一化标题：去掉括号注释（常用于附加"延期到X月X日"），再看主体。"""
    t = unicodedata.normalize("NFKC", text or "")
    t = re.sub(r"[（(][^）)]*[）)]", "", t)
    return re.sub(r"[\s\W_]+", "", t.lower())


def _same_task(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 4 and short in long_


_CN_NUM = {
    "一": "1", "二": "2", "三": "3", "四": "4", "五": "5",
    "六": "6", "七": "7", "八": "8", "九": "9", "十": "10",
    "十一": "11", "十二": "12", "十三": "13", "十四": "14", "十五": "15",
}
_CHAPTER_RE = re.compile(r"第([一二三四五六七八九十]+)章")
_NUM_PREFIX_RE = re.compile(r"^(\d+)\s*[.\-、xX]")
_CATEGORY_RE = re.compile(r"(作业|实验|论文|考试|测验|展示|报告|习题)")


def _set_key(title: str) -> str:
    """把标题归到「第几章 / 第几组」：「第五章作业…」和「5.x 作业」都归到 5。

    作业清单类表格被模型改写标题时（换了措辞、丢了题号），标题对不上但组号对得上。
    """
    t = unicodedata.normalize("NFKC", title or "").strip()
    m = _CHAPTER_RE.search(t)
    if m:
        return _CN_NUM.get(m.group(1), m.group(1))
    m = _NUM_PREFIX_RE.match(t)
    return m.group(1) if m else ""


def _same_day(a: str | None, b: str | None) -> bool:
    """两个 due_at 是否落在同一自然日。

    模型对「晚上12点前」这种说法会在当天 00:00 和 23:59 之间摆动，
    同一天内只差钟点不算改期，否则每次重录都会收到一条假的「改期」。
    """
    if not a or not b:
        return False
    try:
        return datetime.fromisoformat(a).date() == datetime.fromisoformat(b).date()
    except (TypeError, ValueError):
        return False


_COURSE_PREFIX_RE = re.compile(r"^\s*(?:20\d{2}\s*(?:[-—~]\s*20\d{2}\s*)?)\s*[春秋夏冬季]?季?\s*")
_COURSE_SUFFIX_RE = re.compile(r"\s*(?:通知群|交流群|课程群|班群|作业群|群|通知|交流)\s*$")


def normalize_course(course: str | None) -> str | None:
    """把群名/学期前缀从课程名里剥掉。

    模型经常把群名当课程名填：`2026秋电工学` → `电工学`、`工程热力学群` → `工程热力学`。
    不统一的话，指纹里课程这一半对不上，同一份作业会抽成两条。
    """
    if not course:
        return None
    # 不做 NFKC：那会把全角括号改成半角，存进去的课程名会跟历史数据不一致。
    # 归一化比较交给 fingerprint() 里的 _norm()。
    t = str(course).strip()
    t = _COURSE_PREFIX_RE.sub("", t, count=1)
    t = _COURSE_SUFFIX_RE.sub("", t, count=1)
    t = t.strip(" -—_·、,，")
    return t or None


def fingerprint(title: str, course: str | None) -> str:
    """只按标题+课程取指纹：改期时能命中同一条并更新截止时间。"""
    raw = f"{_norm(normalize_course(course) or '')}::{_norm_title(title)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def add_messages(conn: sqlite3.Connection, items: list[dict]) -> int:
    """items: {source, chat, sender, sent_at, content, msg_uid}"""
    added = 0
    ts = now_iso()
    for it in items:
        content = (it.get("content") or "").strip()
        if not content:
            continue
        uid = it.get("msg_uid") or hashlib.sha1(
            f"{it.get('source')}|{it.get('chat')}|{it.get('sent_at')}|{content}".encode()
        ).hexdigest()
        cur = conn.execute(
            "INSERT OR IGNORE INTO messages (source, chat, sender, sent_at, content, msg_uid, ingested_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                it.get("source", "manual"),
                it.get("chat", "手动录入"),
                it.get("sender"),
                it.get("sent_at"),
                content,
                uid,
                ts,
            ),
        )
        added += cur.rowcount
    conn.commit()
    return added


def has_file_message(conn: sqlite3.Connection, source: str, chat: str, name: str) -> bool:
    """这个群里同名文件是否已经解析并入库过，用来避免每天重新下载和解析。

    用 Python 侧前缀比较而不是 SQL LIKE：文件名里的 `_` 和 `%` 在 LIKE 里是通配符。
    """
    prefix = f"file:{source}:{chat}:{name}:"
    rows = conn.execute(
        "SELECT msg_uid FROM messages WHERE source = ? AND chat = ? AND msg_uid LIKE 'file:%'",
        (source, chat),
    ).fetchall()
    return any(str(r[0]).startswith(prefix) for r in rows)


def drop_placeholder(conn: sqlite3.Connection, source: str, chat: str, name: str) -> int:
    """删掉同名群文件的「未解析」占位行（同一文件后来解析成功了）。"""
    cur = conn.execute(
        "DELETE FROM messages WHERE source = ? AND chat = ? AND content = ?",
        (source, chat, f"【群文件】{name}（未解析）"),
    )
    conn.commit()
    return cur.rowcount


def pending_messages(conn: sqlite3.Connection, limit: int = 500) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM messages WHERE extracted = 0 ORDER BY id LIMIT ?", (limit,)
    ).fetchall()


def mark_extracted(conn: sqlite3.Connection, ids: list[int]) -> None:
    if not ids:
        return
    conn.executemany("UPDATE messages SET extracted = 1 WHERE id = ?", [(i,) for i in ids])
    conn.commit()


def messages_by_uids(conn: sqlite3.Connection, uids: list[str]) -> list[dict]:
    """按 msg_uid 取回刚入库的消息行（Telegram 录入回执要拿 id 去抽取）。"""
    uids = [u for u in uids if u]
    if not uids:
        return []
    marks = ",".join("?" * len(uids))
    rows = conn.execute(
        f"SELECT * FROM messages WHERE msg_uid IN ({marks}) ORDER BY id", tuple(uids)
    ).fetchall()
    return [dict(r) for r in rows]


def upsert_task(conn: sqlite3.Connection, t: dict) -> str:
    """返回 created / updated / unchanged。"""
    return upsert_task_detail(conn, t)["action"]


def upsert_task_detail(conn: sqlite3.Connection, t: dict) -> dict:
    """同 upsert_task，但把合并细节一起带回来，供回执展示。

    返回 {"action", "task_id", "title", "course", "due_at", "previous_due"}。
    previous_due 只在改期时有值。
    """
    course = normalize_course(t.get("course"))
    fp = fingerprint(t["title"], course)
    ts = now_iso()
    due = t.get("due_at")
    row = conn.execute("SELECT * FROM tasks WHERE fingerprint = ?", (fp,)).fetchone()

    if row is None:
        # 指纹没命中时，再按标题主体做一次包含式匹配，兜住"实验报告3"与
        # "实验报告3（延期至11月10日）"这类被模型加了注释尾巴的情况。
        wanted = _norm_title(t["title"])
        wanted_course = _norm(course or "")
        wanted_set = _set_key(t["title"])
        wanted_cat = _CATEGORY_RE.search(t["title"])
        for cand in conn.execute("SELECT * FROM tasks WHERE status = 'open'").fetchall():
            cand_course = _norm(normalize_course(cand["course"]) or "")
            if wanted_course and cand_course and wanted_course != cand_course:
                continue
            if _same_task(wanted, _norm_title(cand["title"])):
                row = cand
                break
            # 同课程 + 同截止日 + 同一组号：同一条作业被模型换了个说法
            if not (wanted_set and wanted_set == _set_key(cand["title"])):
                continue
            if (cand["due_at"] or None) != (due or None):
                continue
            cand_cat = _CATEGORY_RE.search(cand["title"] or "")
            # 「第五章作业」和「第五章实验报告」不是一回事，类别不同就不合并
            if wanted_cat and cand_cat and wanted_cat.group(1) != cand_cat.group(1):
                continue
            row = cand
            break

    if row is None:
        cur = conn.execute(
            "INSERT INTO tasks (fingerprint, title, course, kind, due_at, due_text, confidence,"
            " evidence, source, source_ref, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                fp,
                t["title"],
                course,
                t.get("kind"),
                due,
                t.get("due_text"),
                t.get("confidence"),
                t.get("evidence"),
                t.get("source"),
                t.get("source_ref"),
                ts,
                ts,
            ),
        )
        conn.commit()
        return {
            "action": "created",
            "task_id": cur.lastrowid,
            "title": t["title"],
            "course": course,
            "due_at": due,
            "previous_due": None,
        }

    base = {
        "task_id": row["id"],
        "title": row["title"],
        "course": row["course"] or course,
        "due_at": due,
        "previous_due": None,
    }

    # 这次没抽出截止时间（due=None）时不要把已有的时间抹掉——
    # 那条时间可能是用户手动填的（「时间待确认」的作业补上日期后，又重跑了一次抽取）
    keep_due = due is None and bool(row["due_at"])
    if (row["due_at"] or None) == (due or None) or _same_day(row["due_at"], due) or keep_due:
        # 内容没变，只补全缺失字段；同一天内只差钟点的浮动直接对齐，不算改期
        conn.execute(
            "UPDATE tasks SET due_at = COALESCE(?, due_at), confidence = COALESCE(?, confidence),"
            " evidence = COALESCE(?, evidence), course = COALESCE(course, ?),"
            " due_text = COALESCE(due_text, ?), updated_at = ? WHERE id = ?",
            (due, t.get("confidence"), t.get("evidence"), course, t.get("due_text"), ts, row["id"]),
        )
        conn.commit()
        return {**base, "action": "unchanged"}

    history = json.loads(row["history"] or "[]")
    history.append({"at": ts, "from": row["due_at"], "to": due, "why": t.get("evidence", "")[:120]})
    conn.execute(
        "UPDATE tasks SET due_at = ?, due_text = COALESCE(?, due_text), confidence = COALESCE(?, confidence),"
        " evidence = COALESCE(?, evidence), history = ?, updated_at = ?, status = CASE WHEN status = 'done'"
        " THEN 'open' ELSE status END WHERE id = ?",
        (due, t.get("due_text"), t.get("confidence"), t.get("evidence"), json.dumps(history, ensure_ascii=False), ts, row["id"]),
    )
    conn.commit()
    return {**base, "action": "updated", "previous_due": row["due_at"]}


def list_tasks(conn: sqlite3.Connection, status: str = "open") -> list[dict]:
    sql = "SELECT * FROM tasks"
    args: tuple = ()
    if status and status != "all":
        sql += " WHERE status = ?"
        args = (status,)
    sql += " ORDER BY due_at IS NULL, due_at ASC, id ASC"
    out = []
    for r in conn.execute(sql, args):
        d = dict(r)
        d["history"] = json.loads(d.get("history") or "[]")
        out.append(d)
    return out


def set_status(conn: sqlite3.Connection, task_id: int, status: str) -> bool:
    cur = conn.execute("UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?", (status, now_iso(), task_id))
    conn.commit()
    return cur.rowcount > 0


EDITABLE_FIELDS = {"title", "course", "kind", "due_at", "due_text"}
# title 在 SCHEMA 里是 NOT NULL，清空它没有意义，直接拒绝
CLEARABLE_FIELDS = {"course", "kind", "due_at", "due_text"}
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$")


def _normalize_due(value) -> tuple[str | None, str | None]:
    """把用户输入归一成带 +08:00 的 ISO 字符串，返回 (iso, 错误码)。

    无时区的时间按 db.TZ 解释（不是机器时区）；只有日期的补 23:59，
    与 llm.py 里给模型的规则保持一致。
    """
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, "invalid_due"
    s = value.strip()
    if not s:
        return None, None

    date_only = bool(_DATE_ONLY_RE.match(s))
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None, "invalid_due"

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    else:
        dt = dt.astimezone(TZ)

    if date_only:
        dt = dt.replace(hour=23, minute=59, second=0, microsecond=0)
    else:
        dt = dt.replace(microsecond=0)

    now = datetime.now(TZ)
    if not (now.year - 2 <= dt.year <= now.year + 3):
        return None, "invalid_due"
    return dt.isoformat(timespec="seconds"), None


def _row_to_task(row: sqlite3.Row) -> dict:
    d = dict(row)
    try:
        d["history"] = json.loads(d.get("history") or "[]")
    except (ValueError, TypeError):
        d["history"] = []
    return d


def update_task(
    conn: sqlite3.Connection,
    task_id: int,
    fields: dict,
    clear: tuple | list = (),
    why: str = "手动修改",
) -> dict:
    """手动改一条任务，返回结构化结果。

    fields 里放要写入的值；clear 里放要置空的字段名。成功时回
    {"ok": True, "task", "previous_due", "changed"}，失败时回
    {"ok": False, "code": not_found|invalid_field|invalid_due|conflict, "error"}。
    """
    fields = dict(fields or {})
    clear = tuple(clear or ())

    unknown = set(fields) - EDITABLE_FIELDS
    if unknown:
        return {"ok": False, "code": "invalid_field",
                "error": "不支持修改：" + "、".join(sorted(unknown))}
    bad_clear = set(clear) - CLEARABLE_FIELDS
    if bad_clear:
        return {"ok": False, "code": "invalid_field",
                "error": "这些字段不能清空：" + "、".join(sorted(bad_clear))}
    if set(fields) & set(clear):
        return {"ok": False, "code": "invalid_field", "error": "同一字段不能既赋值又清空"}

    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return {"ok": False, "code": "not_found", "error": "任务不存在"}

    new: dict = {}

    if "title" in fields:
        title = str(fields["title"] or "").strip()
        if not title:
            return {"ok": False, "code": "invalid_field", "error": "标题不能为空"}
        new["title"] = title[:120]

    for f in ("course", "kind", "due_text"):
        if f in fields:
            v = fields[f]
            v = "" if v is None else str(v).strip()
            if f == "course":
                v = normalize_course(v) or ""
            new[f] = (v or None)
        elif f in clear:
            new[f] = None

    if "due_at" in fields or "due_at" in clear:
        iso, err = _normalize_due(fields.get("due_at"))
        if err:
            return {"ok": False, "code": "invalid_due",
                    "error": "截止时间格式不对，用 2026-11-25 或 2026-11-25 23:59"}
        new["due_at"] = iso

    changes = {k: v for k, v in new.items() if (row[k] or None) != (v or None)}
    if not changes:
        return {
            "ok": True,
            "task": _row_to_task(row),
            "previous_due": None,
            "previous_status": row["status"],
            "changed": [],
        }

    old_fp = row["fingerprint"]
    new_fp = fingerprint(changes.get("title", row["title"]), changes.get("course", row["course"]))
    if new_fp != old_fp:
        other = conn.execute(
            "SELECT id, title, course, status FROM tasks WHERE fingerprint = ? AND id <> ?",
            (new_fp, task_id),
        ).fetchone()
        if other is not None:
            return {
                "ok": False,
                "code": "conflict",
                "error": f"已存在同名作业（#{other['id']} {other['title']}）",
                "conflict": {
                    "id": other["id"],
                    "title": other["title"],
                    "course": other["course"],
                    "status": other["status"],
                },
            }

    previous_due = row["due_at"]
    due_changed = "due_at" in changes

    sets = dict(changes)
    if new_fp != old_fp:
        sets["fingerprint"] = new_fp
    if due_changed:
        history = json.loads(row["history"] or "[]")
        history.append({
            "at": now_iso(),
            "from": previous_due,
            "to": changes["due_at"],
            "why": why,
        })
        sets["history"] = json.dumps(history, ensure_ascii=False)
        if changes["due_at"]:
            sets["confidence"] = max(row["confidence"] or 0.0, 0.9)
            if row["status"] == "done":
                sets["status"] = "open"
    sets["updated_at"] = now_iso()

    cols = ", ".join(f"{k} = ?" for k in sets)
    try:
        conn.execute(f"UPDATE tasks SET {cols} WHERE id = ?", (*sets.values(), task_id))
    except sqlite3.IntegrityError:
        # 预检之后、写入之前别的线程插了同指纹的行
        conn.rollback()
        return {"ok": False, "code": "conflict", "error": "已存在同名作业，请换个标题"}
    conn.commit()

    fresh = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return {
        "ok": True,
        "task": _row_to_task(fresh),
        "previous_due": previous_due,
        "previous_status": row["status"],
        "changed": sorted(changes),
    }


def log_run(conn: sqlite3.Connection, started_at: str, ok: bool, detail: str) -> None:
    conn.execute(
        "INSERT INTO runs (started_at, finished_at, ok, detail) VALUES (?,?,?,?)",
        (started_at, now_iso(), 1 if ok else 0, detail[:2000]),
    )
    conn.commit()
