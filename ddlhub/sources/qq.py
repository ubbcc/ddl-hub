"""QQ 群：通过 NapCat（OneBot v11 HTTP）拉群消息，含群文件。

NapCat 需要单独装并用小号扫码登录，它对外暴露一个 HTTP 服务（默认
127.0.0.1:3000）。这里只做轮询拉取，不需要反向 WS。

群文件拿不到直链时调 get_group_file_url 换一个，再下载交给 attach 解析。
"""

from __future__ import annotations

import re
from pathlib import Path

import requests

from .. import attach, db

FILE_DIR = Path(__file__).resolve().parents[2] / "data" / "files"
MAX_FILES = 5


def _api(base: str, token: str, action: str, payload: dict | None = None, timeout: int = 30):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"{base.rstrip('/')}/{action}"
    r = requests.post(url, json=payload or {}, headers=headers, timeout=timeout)
    if r.status_code in (401, 403):
        raise RuntimeError(f"NapCat 拒绝访问（{action}），检查 config 里的 token")
    r.raise_for_status()
    data = r.json()
    if data.get("retcode") not in (0, None) or data.get("status") == "failed":
        raise RuntimeError(f"{action} 失败：{data.get('wording') or data.get('message') or data.get('retcode')}")
    return data.get("data")


def _segments(msg: dict) -> list[dict]:
    segs = msg.get("message")
    if isinstance(segs, list):
        return [s for s in segs if isinstance(s, dict)]
    return []


def _text_of(msg: dict) -> str:
    parts = []
    for s in _segments(msg):
        t = s.get("type")
        d = s.get("data") or {}
        if t == "text":
            parts.append(str(d.get("text", "")))
        elif t == "at":
            qq = str(d.get("qq", ""))
            if qq and qq != "all":
                parts.append(f"@{qq}")
    text = "".join(parts).strip()
    if not text and msg.get("raw_message"):
        text = re.sub(r"\[CQ:[^\]]+\]", "", str(msg["raw_message"])).strip()
    return text


def _file_segments(msg: dict) -> list[dict]:
    out = []
    for s in _segments(msg):
        if s.get("type") != "file":
            continue
        d = s.get("data") or {}
        name = d.get("file") or d.get("name") or ""
        if not name:
            continue
        out.append({"name": str(name), "file_id": d.get("file_id") or d.get("id"), "url": d.get("url")})
    return out


def _download(url: str, name: str) -> Path | None:
    if not url:
        return None
    try:
        r = requests.get(url, timeout=60)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"[qq] 下载失败 {name}：{e}")
        return None
    if len(r.content) > attach.MAX_BYTES:
        print(f"[qq] 跳过 {name}：文件过大")
        return None
    return attach.save_bytes(r.content, name, FILE_DIR)


def _resolve_url(base: str, token: str, gid, seg: dict) -> str:
    if seg.get("url"):
        return seg["url"]
    if not seg.get("file_id"):
        return ""
    try:
        data = _api(base, token, "get_group_file_url", {"group_id": gid, "file_id": seg["file_id"]})
    except Exception as e:
        print(f"[qq] 取文件直链失败 {seg['name']}：{e}")
        return ""
    return (data or {}).get("url", "") if isinstance(data, dict) else ""


def collect(conn, cfg, sec) -> dict:
    base = sec.get("onebot_http") or "http://127.0.0.1:3000"
    token = sec.get("token") or ""
    wanted = {str(g) for g in (sec.get("groups") or [])}
    count = int(sec.get("history_count") or 100)

    groups = _api(base, token, "get_group_list") or []
    if not isinstance(groups, list):
        raise RuntimeError("get_group_list 返回格式不对，NapCat 版本可能不兼容")

    rows: list[dict] = []
    files = 0
    for g in groups:
        gid = g.get("group_id")
        name = str(g.get("group_name") or gid)
        if wanted and name not in wanted and str(gid) not in wanted:
            continue

        try:
            hist = _api(base, token, "get_group_msg_history", {"group_id": gid, "count": count}) or {}
        except Exception as e:
            print(f"[qq] 跳过群 {name}：{e}")
            continue
        msgs = hist.get("messages") if isinstance(hist, dict) else None
        if not isinstance(msgs, list):
            continue

        for m in msgs:
            if not isinstance(m, dict):
                continue
            sender = (m.get("sender") or {}).get("card") or (m.get("sender") or {}).get("nickname") or ""
            sent_at = db.now_iso()[:16]
            if m.get("time"):
                try:
                    from datetime import datetime

                    sent_at = datetime.fromtimestamp(int(m["time"]), db.TZ).strftime("%Y-%m-%d %H:%M")
                except (ValueError, OSError, TypeError):
                    pass

            text = _text_of(m)
            if text:
                rows.append(
                    {
                        "source": "qq",
                        "chat": name,
                        "sender": sender,
                        "sent_at": sent_at,
                        "content": text,
                        "msg_uid": f"qq:{gid}:{m.get('message_id')}",
                    }
                )

            if files >= MAX_FILES:
                continue
            for seg in _file_segments(m):
                url = _resolve_url(base, token, gid, seg)
                local = _download(url, seg["name"])
                if not local:
                    continue
                row = attach.to_message(local, "qq", name, sender, sent_at, filename=seg["name"])
                if row:
                    rows.append(row)
                    files += 1

    return {"added": db.add_messages(conn, rows), "scanned": len(rows), "files": files}
