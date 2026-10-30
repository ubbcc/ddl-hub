"""Telegram 手动录入作业。

流程：
    /add_homework        开一个录入会话（也接受 /add-homework 这种写法）
    接着发文字 / 截图 / 文件   先攒着，不立刻解析
    /done                 统一解析、入库、与已有作业合并，回报「原始解析」+「合并结果」
    /cancel               丢弃当前会话

用长轮询 getUpdates，不需要公网地址。offset 存在 data/tg_offset.json，重启不重放。
默认复用提醒用的那个 bot（`notify.telegram.token`）：它只发消息、从不拉更新，
getUpdates 槽位是空的，拿来收消息正好。只有当你另外配了 `intake.token` 才用新 bot——
但那个 bot 不能被别的程序同时长轮询，否则两边会抢同一条更新。
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from pathlib import Path

import requests

from . import attach, db, pipeline

API = "https://api.telegram.org/bot{token}/{method}"
DATA_DIR = Path(__file__).resolve().parents[1] / "data"
FILE_DIR = DATA_DIR / "files"
OFFSET_PATH = DATA_DIR / "tg_offset.json"
CHAT_LABEL = "Telegram 录入"

START_CMDS = {"add_homework", "add", "addhomework"}
DONE_CMDS = {"done", "ok", "好", "完成"}
CANCEL_CMDS = {"cancel", "取消", "abort"}
HELP_CMDS = {"help", "start", "帮助"}

HELP_TEXT = (
    "发送 /add_homework 开始录入，然后直接发作业消息、截图或文件，"
    "最后发 /done 解析。中途 /cancel 丢弃。"
)


# ---------------------------------------------------------------- Telegram

def _api(token: str, method: str, payload: dict | None = None, timeout: int = 30) -> dict:
    try:
        r = requests.post(API.format(token=token, method=method), json=payload or {}, timeout=timeout)
    except requests.RequestException as e:
        raise RuntimeError(f"{method} 请求失败：{e}") from None
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(f"{method} 返回的不是 JSON（HTTP {r.status_code}）") from None
    if not data.get("ok"):
        raise RuntimeError(f"{method} 失败：{data.get('description') or r.status_code}")
    return data


def _send(token: str, chat_id: str, text: str) -> None:
    for i in range(0, len(text), 3800):
        _api(token, "sendMessage", {
            "chat_id": chat_id,
            "text": text[i : i + 3800],
            "disable_web_page_preview": True,
        })


def _load_offset() -> int:
    try:
        return int(OFFSET_PATH.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return 0


def _save_offset(offset: int) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        OFFSET_PATH.write_text(str(offset), encoding="utf-8")
    except OSError as e:
        print(f"[intake] 记录 offset 失败：{e}")


def _download(token: str, file_id: str, name: str) -> Path | None:
    """把 Telegram 上的文件拉到本地 data/files。超过 attach 上限就放弃。"""
    try:
        info = _api(token, "getFile", {"file_id": file_id})
        rel = (info.get("result") or {}).get("file_path")
        if not rel:
            return None
        r = requests.get(f"https://api.telegram.org/file/bot{token}/{rel}", timeout=120)
        r.raise_for_status()
    except (requests.RequestException, RuntimeError) as e:
        print(f"[intake] 下载 {name} 失败：{e}")
        return None
    if len(r.content) > attach.MAX_BYTES:
        print(f"[intake] {name} 超过 {attach.MAX_BYTES // 1024 // 1024} MB，跳过")
        return None
    return attach.save_bytes(r.content, name, FILE_DIR)


# ---------------------------------------------------------------- 会话

def _new_session(now: datetime) -> dict:
    return {"items": [], "started": now}


def _command(text: str) -> str:
    """把 '/add-homework xxx' 归一成 'add_homework'。"""
    t = (text or "").strip()
    if not t.startswith("/"):
        return ""
    head = t[1:].split(maxsplit=1)[0].split("@")[0].strip().lower()
    return head.replace("-", "_")


def _who(msg: dict) -> str:
    u = msg.get("from") or {}
    return u.get("username") or u.get("first_name") or str(u.get("id") or "我")


def _stamp(msg: dict) -> str:
    ts = msg.get("date")
    if ts:
        try:
            return datetime.fromtimestamp(int(ts), db.TZ).strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError, TypeError, OverflowError):
            pass
    return db.now_iso()[:16]


def _collect(token: str, msg: dict, session: dict) -> str | None:
    """把一条消息收进会话，返回给用户的即时反馈（None 表示不反馈）。"""
    sender = _who(msg)
    at = _stamp(msg)
    chat_id = str((msg.get("chat") or {}).get("id"))
    mid = msg.get("message_id")

    text = (msg.get("text") or "").strip()
    caption = (msg.get("caption") or "").strip()
    photo = msg.get("photo")
    doc = msg.get("document")

    if text and not photo and not doc:
        session["items"].append({
            "kind": "text", "text": text, "sender": sender,
            "at": at, "chat_id": chat_id, "message_id": mid,
        })
        return f"📝 已收下一条文字（共 {len(session['items'])} 条），继续发或 /done"

    if photo:
        biggest = max(photo, key=lambda p: p.get("file_size") or 0)
        name = f"telegram_{biggest.get('file_unique_id') or mid}.jpg"
        local = _download(token, biggest.get("file_id"), name)
        if not local:
            return "⚠️ 图片下载失败，换一张或稍后再试"
        if caption:
            session["items"].append({
                "kind": "text", "text": caption, "sender": sender,
                "at": at, "chat_id": chat_id, "message_id": mid,
            })
        session["items"].append({
            "kind": "file", "path": local, "name": name, "sender": sender,
            "at": at, "chat_id": chat_id, "message_id": mid,
        })
        return f"🖼 已收下图片（共 {len(session['items'])} 条），继续发或 /done"

    if doc:
        name = doc.get("file_name") or f"telegram_{mid}"
        local = _download(token, doc.get("file_id"), name)
        if not local:
            return "⚠️ 文件下载失败（可能超过 20 MB），换个方式发"
        if caption:
            session["items"].append({
                "kind": "text", "text": caption, "sender": sender,
                "at": at, "chat_id": chat_id, "message_id": mid,
            })
        session["items"].append({
            "kind": "file", "path": local, "name": name, "sender": sender,
            "at": at, "chat_id": chat_id, "message_id": mid,
        })
        return f"📎 已收下文件 {name}（共 {len(session['items'])} 条），继续发或 /done"

    return None


# ---------------------------------------------------------------- 收尾

def _to_rows(session: dict) -> tuple[list[dict], list[str]]:
    """会话内容 → 可入库的消息行，同时收集附件解析情况。"""
    rows: list[dict] = []
    notes: list[str] = []
    for it in session["items"]:
        uid_base = f"tg:{it['chat_id']}:{it['message_id']}"
        if it["kind"] == "text":
            rows.append({
                "source": "telegram",
                "chat": CHAT_LABEL,
                "sender": it["sender"],
                "sent_at": it["at"],
                "content": it["text"],
                "msg_uid": uid_base,
            })
            continue

        row = attach.to_message(
            it["path"], "telegram", CHAT_LABEL, it["sender"], it["at"], filename=it["name"]
        )
        if row:
            notes.append(f"📎 {it['name']}：{_engine_of(row)}")
        else:
            row = {
                "source": "telegram",
                "chat": CHAT_LABEL,
                "sender": it["sender"],
                "sent_at": it["at"],
                "content": f"【附件】{it['name']}（未解析）",
                "msg_uid": f"{uid_base}:file",
            }
            notes.append(f"📎 {it['name']}：解析失败，只记了文件名")
        rows.append(row)
    return rows, notes


ENGINE_LABEL = {
    "text": "文本",
    "docx": "Word(docx)",
    "word-com": "Word",
    "pymupdf": "PDF",
    "python-pptx": "PPT",
    "openpyxl": "Excel",
    "claude-remote": "claude 解析",
}


def _engine_of(row: dict) -> str:
    content = row.get("content") or ""
    head = content.split("\n", 1)[0]
    if "解析方式：" in head:
        engine = head.split("解析方式：", 1)[1].rstrip("）)")
        return ENGINE_LABEL.get(engine, engine)
    return "已解析"


def _fmt_due(due_at: str | None) -> str:
    if not due_at:
        return "时间未定"
    try:
        dt = datetime.fromisoformat(due_at)
    except ValueError:
        return due_at
    now = datetime.now(db.TZ)
    delta = dt - now
    mins = int(delta.total_seconds() // 60)
    if mins < 0:
        days = -mins // 1440
        return f"{dt:%m-%d %H:%M}（逾期 {days} 天）" if days else f"{dt:%m-%d %H:%M}（已逾期）"
    if mins < 1440:
        return f"{dt:%m-%d %H:%M}（{mins // 60} 小时后）"
    return f"{dt:%m-%d %H:%M}（{mins // 1440} 天后）"


def _report(result: dict, notes: list[str], added: int, cfg: dict) -> str:
    raw = result.get("raw") or []
    merged = result.get("tasks") or []
    lines: list[str] = []

    if not raw:
        lines.append("🔍 这几条消息里没解析出带明确时限的作业。")
    else:
        lines.append(f"📥 原始解析：{len(raw)} 条作业")
        for i, t in enumerate(raw, 1):
            head = t.get("title") or "(无标题)"
            meta = [x for x in (t.get("course"), t.get("kind")) if x]
            lines.append("")
            lines.append(f"{i}. {head}")
            if meta:
                lines.append(f"   {' · '.join(meta)} ｜ {_fmt_due(t.get('due_at'))}")
            else:
                lines.append(f"   {_fmt_due(t.get('due_at'))}")
            lines.append(f"   置信 {t.get('confidence')}")
            ev = (t.get("evidence") or "").replace("\n", " ").strip()
            if ev:
                lines.append(f"   依据：{ev[:80]}")

    if merged:
        created = [m for m in merged if m.get("action") == "created"]
        updated = [m for m in merged if m.get("action") == "updated"]
        unchanged = [m for m in merged if m.get("action") == "unchanged"]
        lines.append("")
        lines.append("🔀 合并结果")
        if created:
            lines.append(f"➕ 新增 {len(created)}")
            for m in created:
                lines.append(f"   · {m.get('title')}")
        if updated:
            lines.append(f"🔁 改期 {len(updated)}")
            for m in updated:
                lines.append(f"   · {m.get('title')}：{m.get('previous_due') or '无'} → {m.get('due_at')}")
        if unchanged:
            lines.append(f"♻️ 已有 {len(unchanged)}，未重复添加")
            for m in unchanged:
                lines.append(f"   · {m.get('title')}")

    if notes:
        lines.append("")
        lines.extend(notes)

    if added == 0 and not raw:
        lines.append("")
        lines.append("（这些内容之前录入过，已跳过）")

    server = (cfg.get("server") or {})
    host = server.get("host") or "127.0.0.1"
    port = server.get("port") or 8848
    lines.append("")
    lines.append(f"看板：http://{host}:{port}")
    return "\n".join(lines).strip()


def _finish(token: str, chat_id: str, session: dict, cfg: dict) -> None:
    rows, notes = _to_rows(session)
    if not rows:
        _send(token, chat_id, "会话是空的，什么都没录入。")
        return

    conn = db.connect()
    try:
        added = db.add_messages(conn, rows)
        uids = [r.get("msg_uid") for r in rows if r.get("msg_uid")]
        stored = db.messages_by_uids(conn, uids)
        if not stored:
            _send(token, chat_id, "这些内容之前已经录入过，没有新增。")
            return
        result = pipeline.extract_rows(conn, stored, cfg)
    except Exception as e:  # 解析失败也要把情况说清楚
        print(f"[intake] 解析失败：{e}")
        _send(token, chat_id, f"⚠️ 解析失败：{str(e)[:400]}")
        return
    finally:
        conn.close()

    _send(token, chat_id, _report(result, notes, added, cfg))


# ---------------------------------------------------------------- 主循环

def _handle(token: str, update: dict, cfg: dict, sessions: dict, timeout_min: int) -> None:
    msg = update.get("message") or update.get("edited_message") or {}
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    if not chat_id:
        return

    allowed = {str(x) for x in (cfg.get("intake", {}).get("allowed_chat_ids") or [])}
    if not allowed:
        # 没显式配置就退回到提醒用的那个 chat_id，避免 bot 是公开的、谁都能往里塞作业
        fallback = os.environ.get("DDLHUB_TG_CHAT") or (
            ((cfg.get("notify") or {}).get("telegram") or {}).get("chat_id")
        )
        if fallback:
            allowed = {str(fallback)}
    if not allowed:
        print("[intake] 没有可用的白名单 chat_id，拒绝处理（配置 intake.allowed_chat_ids）")
        return
    if chat_id not in allowed:
        print(f"[intake] 忽略未授权 chat_id {chat_id}")
        return

    text = msg.get("text") or ""
    cmd = _command(text)

    # 会话过期就当没开过，避免几天后突然冒出一堆陈年消息
    session = sessions.get(chat_id)
    if session:
        age = (datetime.now(db.TZ) - session["started"]).total_seconds() / 60
        if age > timeout_min:
            sessions.pop(chat_id, None)
            _send(token, chat_id, "上一轮录入会话已超时，这次的内容没保存。要录重新发 /add_homework。")
            session = None

    if cmd in START_CMDS:
        sessions[chat_id] = _new_session(datetime.now(db.TZ))
        _send(token, chat_id, "开始录入。发作业消息、截图或文件，完成后发 /done。")
        return

    if cmd in HELP_CMDS:
        _send(token, chat_id, HELP_TEXT)
        return

    if cmd in CANCEL_CMDS:
        if sessions.pop(chat_id, None):
            _send(token, chat_id, "已丢弃本轮录入。")
        else:
            _send(token, chat_id, "当前没有进行中的录入。")
        return

    if cmd in DONE_CMDS:
        if not session:
            _send(token, chat_id, "当前没有进行中的录入，先发 /add_homework。")
            return
        sessions.pop(chat_id, None)
        _send(token, chat_id, f"⏳ 收到 {len(session['items'])} 条内容，开始解析…")
        _finish(token, chat_id, session, cfg)
        return

    if not session:
        _send(token, chat_id, "没在录入模式。" + HELP_TEXT)
        return

    feedback = _collect(token, msg, session)
    if feedback:
        _send(token, chat_id, feedback)


def _token_of(cfg: dict) -> str:
    """录入用的 bot token：优先 intake.token，没填就复用提醒那个 bot。"""
    intake_cfg = cfg.get("intake") or {}
    return (
        intake_cfg.get("token")
        or ((cfg.get("notify") or {}).get("telegram") or {}).get("token")
        or ""
    )


def run_forever(cfg: dict) -> None:
    token = _token_of(cfg)
    if not token:
        print("[intake] 没有可用的 bot token（intake.token 和 notify.telegram.token 都是空的），手动录入不启动")
        return

    timeout_min = int((cfg.get("intake") or {}).get("session_timeout_minutes") or 30)
    sessions: dict[str, dict] = {}
    offset = _load_offset()

    if not OFFSET_PATH.exists():
        # 首次启动不要把历史积压的更新一条条回放：取最后一条的 id 直接跳过
        try:
            tail = _api(token, "getUpdates", {"offset": -1, "limit": 1})
            got = tail.get("result") or []
            offset = int(got[-1]["update_id"]) + 1 if got else 0
            _save_offset(offset)
            print(f"[intake] 首次启动，跳过历史更新，从 offset={offset} 开始")
        except Exception as e:
            print(f"[intake] 读取历史 offset 失败（从 0 开始）：{e}")

    try:
        _api(token, "setMyCommands", {"commands": [
            {"command": "add_homework", "description": "开始录入作业"},
            {"command": "done", "description": "解析并写进看板"},
            {"command": "cancel", "description": "丢弃本轮录入"},
            {"command": "help", "description": "使用说明"},
        ]})
    except Exception as e:
        print(f"[intake] 设置命令菜单失败（不影响录入）：{e}")

    print(f"[intake] 开始长轮询（offset={offset}）", flush=True)

    while True:
        try:
            data = _api(token, "getUpdates", {
                "offset": offset,
                "timeout": 25,
                "allowed_updates": ["message", "edited_message"],
            }, timeout=45)
        except Exception as e:
            print(f"[intake] 拉取更新失败：{e}", flush=True)
            time.sleep(5)
            continue

        for up in data.get("result") or []:
            offset = int(up.get("update_id", offset)) + 1
            _save_offset(offset)
            try:
                _handle(token, up, cfg, sessions, timeout_min)
            except Exception as e:
                print(f"[intake] 处理 update {up.get('update_id')} 出错：{e}", flush=True)


def start(cfg: dict) -> threading.Thread | None:
    """在 run.py 里当后台线程起。没启用就返回 None。"""
    if not (cfg.get("intake") or {}).get("enabled"):
        return None
    t = threading.Thread(target=run_forever, args=(cfg,), daemon=True)
    t.start()
    return t


def _selfcheck() -> None:
    """不联网的自检：确认导入和各辅助函数没坏。"""
    assert _command("/add-homework") == "add_homework"
    assert _command("/add_homework@my_bot 参数") == "add_homework"
    assert _command("/DONE") == "done"
    assert _command("随便一句话") == ""
    assert _fmt_due(None) == "时间未定"
    print("intake selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
