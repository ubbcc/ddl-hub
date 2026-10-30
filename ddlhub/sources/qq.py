"""QQ 群：通过 NapCat（OneBot v11 HTTP）拉群消息，含群文件。

NapCat 需要单独装并用小号扫码登录，它对外暴露一个 HTTP 服务（默认
127.0.0.1:3000）。这里只做轮询拉取，不需要反向 WS。

群文件拿不到直链时调 get_group_file_url 换一个，再下载交给 attach 解析。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from urllib.parse import unquote

import requests

from .. import attach, db

FILE_DIR = Path(__file__).resolve().parents[2] / "data" / "files"
MAX_FILES = 5


def _api(base: str, token: str, action: str, payload: dict | None = None, timeout: int = 30):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"{base.rstrip('/')}/{action}"
    try:
        r = requests.post(url, json=payload or {}, headers=headers, timeout=timeout)
    except requests.ConnectionError:
        raise RuntimeError(
            f"连不上 NapCat（{base}）。确认 NapCat 已启动，且 onebot11 配置里的 HTTP 服务端口一致"
        ) from None
    if r.status_code in (401, 403):
        raise RuntimeError(f"NapCat 拒绝访问（{action}），检查 config 里的 token")
    r.raise_for_status()
    data = r.json()
    if data.get("retcode") not in (0, None) or data.get("status") == "failed":
        raise RuntimeError(f"{action} 失败：{data.get('wording') or data.get('message') or data.get('retcode')}")
    return data.get("data")


_CQ_RE = re.compile(r"\[CQ:([a-zA-Z_]+)((?:,[^\]]*)?)\]")


def _cq_unescape(s: str) -> str:
    return (
        s.replace("&#91;", "[")
        .replace("&#93;", "]")
        .replace("&#44;", ",")
        .replace("&amp;", "&")
    )


def _parse_cq(text: str) -> list[dict]:
    """把 CQ 码字符串拆成 segment 列表（messagePostFormat=string 的版本）。"""
    out, pos = [], 0
    for m in _CQ_RE.finditer(text):
        if m.start() > pos:
            out.append({"type": "text", "data": {"text": _cq_unescape(text[pos : m.start()])}})
        data = {}
        for kv in m.group(2).lstrip(",").split(","):
            if "=" in kv:
                k, v = kv.split("=", 1)
                data[k] = _cq_unescape(v)
        out.append({"type": m.group(1), "data": data})
        pos = m.end()
    if pos < len(text):
        out.append({"type": "text", "data": {"text": _cq_unescape(text[pos:])}})
    return out


def _segments(msg: dict) -> list[dict]:
    segs = msg.get("message")
    if isinstance(segs, list):
        return [s for s in segs if isinstance(s, dict)]
    if isinstance(segs, str) and segs:
        return _parse_cq(segs)
    raw = msg.get("raw_message")
    if isinstance(raw, str) and raw:
        return _parse_cq(raw)
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


_FILE_ID_KEYS = ("file_id", "fid", "id")


def _file_segments(msg: dict) -> list[dict]:
    out = []
    for s in _segments(msg):
        if s.get("type") != "file":
            continue
        d = s.get("data") or {}
        name = str(d.get("file") or d.get("name") or d.get("file_name") or "")
        url = str(d.get("url") or "")
        # 有的版本把直链塞在 file 字段里
        if name.startswith(("http://", "https://")):
            url, name = name, unquote(name.split("?")[0].rsplit("/", 1)[-1])
        else:
            name = unquote(name)
        if not name:
            continue
        file_id = next((d[k] for k in _FILE_ID_KEYS if d.get(k)), None)
        out.append({"name": name, "file_id": file_id, "busid": d.get("busid"), "url": url})
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
    payload = {"group_id": gid, "file_id": seg["file_id"]}
    if seg.get("busid") is not None:
        payload["busid"] = seg["busid"]
    try:
        data = _api(base, token, "get_group_file_url", payload)
    except Exception as e:
        print(f"[qq] 取文件直链失败 {seg['name']}：{e}")
        return ""
    return (data or {}).get("url", "") if isinstance(data, dict) else ""


def _placeholder(chat: str, sender: str, sent_at: str, seg: dict) -> dict:
    """下载或解析失败时仍入库一条，让 LLM 知道群里发过这个文件。"""
    key = seg.get("file_id") or seg["name"]
    return {
        "source": "qq",
        "chat": chat,
        "sender": sender or "文件",
        "sent_at": sent_at,
        "content": f"【群文件】{seg['name']}（未解析）",
        "msg_uid": f"file:qq:{chat}:{key}",
    }


def _fmt_ts(ts) -> str:
    from datetime import datetime

    if ts:
        try:
            return datetime.fromtimestamp(int(ts), db.TZ).strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError, TypeError, OverflowError):
            pass
    return datetime.now(db.TZ).strftime("%Y-%m-%d %H:%M")


def collect(conn, cfg, sec) -> dict:
    base = sec.get("onebot_http") or "http://127.0.0.1:3000"
    token = sec.get("token") or ""
    wanted = {str(g) for g in (sec.get("groups") or [])}
    count = int(sec.get("history_count") or 100)

    groups = _api(base, token, "get_group_list") or []
    if not isinstance(groups, list):
        raise RuntimeError("get_group_list 返回格式不对，NapCat 版本可能不兼容")
    if not wanted:
        print(f"[qq] 未配置 groups，本次抓取全部 {len(groups)} 个群")

    rows: list[dict] = []
    files = 0
    seen_groups: set[str] = set()
    for g in groups:
        gid = g.get("group_id")
        name = str(g.get("group_name") or gid)
        seen_groups.add(name)
        seen_groups.add(str(gid))
        if wanted and name not in wanted and str(gid) not in wanted:
            continue

        try:
            hist = _api(
                base,
                token,
                "get_group_msg_history",
                {"group_id": gid, "count": count, "reverseOrder": False},
            ) or {}
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
            sent_at = _fmt_ts(m.get("time"))

            text = _text_of(m)
            if text:
                mid = m.get("message_id")
                if mid is None:
                    mid = hashlib.sha1(f"{m.get('time')}|{sender}|{text}".encode()).hexdigest()[:16]
                rows.append(
                    {
                        "source": "qq",
                        "chat": name,
                        "sender": sender,
                        "sent_at": sent_at,
                        "content": text,
                        "msg_uid": f"qq:{gid}:{mid}",
                    }
                )

            if files >= MAX_FILES:
                continue
            for seg in _file_segments(m):
                # 同名文件之前已经解析入库过就不再重复下载：图片 OCR 走 claude，
                # 每轮重跑代价很高。顺手清掉可能残留的「未解析」占位行。
                if db.has_file_message(conn, "qq", name, seg["name"]):
                    db.drop_placeholder(conn, "qq", name, seg["name"])
                    continue
                url = _resolve_url(base, token, gid, seg)
                local = _download(url, seg["name"]) if url else None
                row = attach.to_message(local, "qq", name, sender, sent_at, filename=seg["name"]) if local else None
                if row:
                    db.drop_placeholder(conn, "qq", name, seg["name"])
                else:
                    row = _placeholder(name, sender, sent_at, seg)
                rows.append(row)
                files += 1

    if wanted:
        missing = wanted - seen_groups
        if missing:
            print(f"[qq] 配置里的群没在 QQ 里找到：{'、'.join(sorted(missing))}")

    return {"added": db.add_messages(conn, rows), "scanned": len(rows), "files": files}
