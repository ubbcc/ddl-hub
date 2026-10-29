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


def fingerprint(title: str, course: str | None) -> str:
    """只按标题+课程取指纹：改期时能命中同一条并更新截止时间。"""
    raw = f"{_norm(course or '')}::{_norm_title(title)}"
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


def pending_messages(conn: sqlite3.Connection, limit: int = 500) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM messages WHERE extracted = 0 ORDER BY id LIMIT ?", (limit,)
    ).fetchall()


def mark_extracted(conn: sqlite3.Connection, ids: list[int]) -> None:
    if not ids:
        return
    conn.executemany("UPDATE messages SET extracted = 1 WHERE id = ?", [(i,) for i in ids])
    conn.commit()


def upsert_task(conn: sqlite3.Connection, t: dict) -> str:
    """返回 created / updated / unchanged。"""
    fp = fingerprint(t["title"], t.get("course"))
    ts = now_iso()
    due = t.get("due_at")
    row = conn.execute("SELECT * FROM tasks WHERE fingerprint = ?", (fp,)).fetchone()

    if row is None:
        # 指纹没命中时，再按标题主体做一次包含式匹配，兜住"实验报告3"与
        # "实验报告3（延期至11月10日）"这类被模型加了注释尾巴的情况。
        wanted = _norm_title(t["title"])
        wanted_course = _norm(t.get("course") or "")
        for cand in conn.execute("SELECT * FROM tasks WHERE status = 'open'").fetchall():
            cand_course = _norm(cand["course"] or "")
            if wanted_course and cand_course and wanted_course != cand_course:
                continue
            if _same_task(wanted, _norm_title(cand["title"])):
                row = cand
                break

    if row is None:
        conn.execute(
            "INSERT INTO tasks (fingerprint, title, course, kind, due_at, due_text, confidence,"
            " evidence, source, source_ref, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                fp,
                t["title"],
                t.get("course"),
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
        return "created"

    if (row["due_at"] or None) == (due or None):
        # 内容没变，只补全缺失字段
        conn.execute(
            "UPDATE tasks SET confidence = COALESCE(?, confidence), evidence = COALESCE(?, evidence),"
            " course = COALESCE(course, ?), due_text = COALESCE(due_text, ?), updated_at = ? WHERE id = ?",
            (t.get("confidence"), t.get("evidence"), t.get("course"), t.get("due_text"), ts, row["id"]),
        )
        conn.commit()
        return "unchanged"

    history = json.loads(row["history"] or "[]")
    history.append({"at": ts, "from": row["due_at"], "to": due, "why": t.get("evidence", "")[:120]})
    conn.execute(
        "UPDATE tasks SET due_at = ?, due_text = COALESCE(?, due_text), confidence = COALESCE(?, confidence),"
        " evidence = COALESCE(?, evidence), history = ?, updated_at = ?, status = CASE WHEN status = 'done'"
        " THEN 'open' ELSE status END WHERE id = ?",
        (due, t.get("due_text"), t.get("confidence"), t.get("evidence"), json.dumps(history, ensure_ascii=False), ts, row["id"]),
    )
    conn.commit()
    return "updated"


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


def update_task(conn: sqlite3.Connection, task_id: int, fields: dict) -> bool:
    allowed = {"title", "course", "kind", "due_at", "due_text"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return False
    sets["updated_at"] = now_iso()
    cols = ", ".join(f"{k} = ?" for k in sets)
    cur = conn.execute(f"UPDATE tasks SET {cols} WHERE id = ?", (*sets.values(), task_id))
    conn.commit()
    return cur.rowcount > 0


def log_run(conn: sqlite3.Connection, started_at: str, ok: bool, detail: str) -> None:
    conn.execute(
        "INSERT INTO runs (started_at, finished_at, ok, detail) VALUES (?,?,?,?)",
        (started_at, now_iso(), 1 if ok else 0, detail[:2000]),
    )
    conn.commit()
