"""微信群：用 wxauto 做只读的界面自动化采集。

不碰协议、不读数据库，只从微信 PC 客户端窗口里读消息，风险最低。
前提是微信 3.9.x —— 4.x 的控件结构变了，wxauto 连不上。

优先用 GetNextNewMessage()（只返回有新消息的会话，顺手消红点），
没有该接口时退回逐群 ChatWith + GetAllMessage 并自行去重。
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

from .. import attach, db

SKIP_TYPES = {"sys", "time", "recall", "base"}
FILE_TYPES = {"file"}

# 微信 PC 端接收文件的默认目录（3.9.x / 4.x 各一套）
DEFAULT_FILE_DIRS = (
    Path.home() / "Documents" / "WeChat Files",
    Path.home() / "Documents" / "xwechat_files",
    Path.home() / "Documents" / "WeChat Files" / "All Users",
)


def _find_local_file(name: str, extra_dirs: list[str] | None = None) -> Path | None:
    """按文件名在微信下载目录里找最近的一份。"""
    if not name:
        return None
    target = Path(name).name
    dirs = [Path(d) for d in (extra_dirs or []) if d] or list(DEFAULT_FILE_DIRS)
    best: Path | None = None
    best_mtime = -1.0
    for root in dirs:
        if not root.exists():
            continue
        try:
            for p in root.rglob(target):
                try:
                    mt = p.stat().st_mtime
                except OSError:
                    continue
                if p.is_file() and mt > best_mtime:
                    best, best_mtime = p, mt
        except OSError:
            continue
    return best


def _uid(chat: str, m) -> str:
    mid = getattr(m, "id", None) or getattr(m, "msgid", None)
    if mid:
        return f"wx:{chat}:{mid}"
    raw = "|".join(
        str(getattr(m, k, "") or "")
        for k in ("sender", "content", "time", "type")
    )
    return "wx:" + hashlib.sha1(f"{chat}|{raw}".encode("utf-8")).hexdigest()[:20]


def _rows(chat: str, msgs, file_dirs: list[str] | None = None) -> list[dict]:
    out = []
    ts = db.now_iso()[:16]
    for m in msgs or []:
        mtype = str(getattr(m, "type", "") or "").lower()
        if mtype in SKIP_TYPES:
            continue
        content = (getattr(m, "content", "") or "").strip()
        if not content:
            continue
        sender = getattr(m, "sender", None) or ""
        sent_at = str(getattr(m, "time", "") or ts)

        if mtype in FILE_TYPES:
            # 文件名单独成行，至少要能让 LLM 知道群里发过这么个文件
            local = _find_local_file(content, file_dirs)
            if local:
                row = attach.to_message(local, "wechat", chat, sender, sent_at, filename=content)
                if row:
                    out.append(row)
                    continue
            out.append(
                {
                    "source": "wechat",
                    "chat": chat,
                    "sender": sender,
                    "sent_at": sent_at,
                    "content": f"【群文件】{content}（本机没找到文件，未解析）",
                    "msg_uid": _uid(chat, m),
                }
            )
            continue

        out.append(
            {
                "source": "wechat",
                "chat": chat,
                "sender": sender,
                "sent_at": sent_at,
                "content": content,
                "msg_uid": _uid(chat, m),
            }
        )
    return out


def _new_messages(wx, wanted: set[str]) -> tuple[list[dict], bool]:
    fn = getattr(wx, "GetNextNewMessage", None)
    if not callable(fn):
        return [], False
    try:
        batch = fn(savepic=False) or {}
    except TypeError:
        try:
            batch = fn() or {}
        except Exception:
            return [], False
    except Exception:
        return [], False

    rows = []
    if isinstance(batch, dict):
        for chat, msgs in batch.items():
            if wanted and chat not in wanted:
                continue
            rows.extend(_rows(str(chat), msgs))
    return rows, True


def _all_messages(wx, wanted: set[str]) -> list[dict]:
    if not wanted:
        return []
    rows = []
    for chat in wanted:
        try:
            if hasattr(wx, "ChatWith"):
                wx.ChatWith(chat)
                time.sleep(0.6)
        except Exception:
            continue
        try:
            msgs = wx.GetAllMessage(savepic=False)
        except TypeError:
            msgs = wx.GetAllMessage()
        except Exception:
            continue
        rows.extend(_rows(chat, msgs))
    return rows


def collect(conn, cfg, sec) -> dict:
    if (sec.get("mode") or "manual") != "wxauto":
        return {"added": 0, "note": "mode 不是 wxauto，跳过"}

    try:
        from wxauto import WeChat
    except ImportError as e:
        raise RuntimeError("没装 wxauto：pip install wxauto（仅支持微信 3.9.x）") from e

    try:
        wx = WeChat()
    except Exception as e:
        raise RuntimeError(
            f"连不上微信客户端。wxauto 只支持微信 3.9.x，当前若是 4.x 需要先降级：{e}"
        ) from e

    wanted = {str(g) for g in (sec.get("groups") or [])}

    rows, used_new_api = _new_messages(wx, wanted)
    if not used_new_api:
        rows = _all_messages(wx, wanted)

    added = db.add_messages(conn, rows)
    return {
        "added": added,
        "scanned": len(rows),
        "api": "GetNextNewMessage" if used_new_api else "GetAllMessage",
    }
