"""把消息跑过 LLM，落到任务表里。"""

from __future__ import annotations

from pathlib import Path

import yaml

from . import db, llm


def load_config(path: Path | str | None = None) -> dict:
    p = Path(path) if path else Path(__file__).resolve().parent.parent / "config.yaml"
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def run_extraction(conn=None, config: dict | None = None, limit: int = 500) -> dict:
    cfg = config or load_config()
    llm_cfg = cfg.get("llm", {})
    backend = llm_cfg.get("backend", "claude-cli")
    timeout = int(llm_cfg.get("timeout_seconds", 240))
    batch_size = max(1, int(llm_cfg.get("batch_size", 60)))

    own = conn is None
    conn = conn or db.connect()
    started = db.now_iso()
    stats = {"messages": 0, "created": 0, "updated": 0, "unchanged": 0, "errors": []}
    try:
        rows = db.pending_messages(conn, limit=limit)
        if not rows:
            db.log_run(conn, started, True, "没有待处理消息")
            return stats

        msgs = [dict(r) for r in rows]
        stats["messages"] = len(msgs)

        for start in range(0, len(msgs), batch_size):
            chunk = msgs[start : start + batch_size]
            _process_chunk(conn, chunk, backend, timeout, stats)

        db.log_run(conn, started, not stats["errors"], str(stats))
        return stats
    finally:
        if own:
            conn.close()


def _process_chunk(conn, chunk: list[dict], backend: str, timeout: int, stats: dict) -> None:
    """整批失败就二分重试——模型偶尔会返回坏 JSON，把坏样本隔离出来，
    避免一批 60 条里有一条脏数据就全批丢弃。"""
    if not chunk:
        return
    try:
        tasks = llm.extract_batch(chunk, backend=backend, timeout=timeout)
    except Exception as e:
        if len(chunk) > 1:
            mid = len(chunk) // 2
            _process_chunk(conn, chunk[:mid], backend, timeout, stats)
            _process_chunk(conn, chunk[mid:], backend, timeout, stats)
            return
        stats["errors"].append(f"第 {chunk[0].get('id')} 条消息抽取失败：{str(e)[:200]}")
        return

    for t in tasks:
        stats[db.upsert_task(conn, t)] += 1
    db.mark_extracted(conn, [m["id"] for m in chunk])


def extract_rows(conn, rows: list[dict], config: dict | None = None) -> dict:
    """只对指定的消息行做抽取 + 合并，不碰其它待处理消息。

    Telegram 手动录入用这个：用户刚发的那几条立刻出结果，不会把群采集攒下来的
    几十条一起拖进来。
    """
    cfg = config or load_config()
    llm_cfg = cfg.get("llm", {})
    backend = llm_cfg.get("backend", "claude-cli")
    timeout = int(llm_cfg.get("timeout_seconds", 240))
    batch_size = max(1, int(llm_cfg.get("batch_size", 60)))

    rows = [r for r in rows if isinstance(r, dict) and r.get("id") is not None]
    result: dict = {"messages": len(rows), "raw": [], "tasks": [], "errors": []}
    if not rows:
        return result

    started = db.now_iso()
    for start in range(0, len(rows), batch_size):
        chunk = rows[start : start + batch_size]
        _process_chunk_detail(conn, chunk, backend, timeout, result)

    db.log_run(conn, started, not result["errors"], f"manual: {result['tasks']}")
    return result


def _process_chunk_detail(conn, chunk: list[dict], backend: str, timeout: int, result: dict) -> None:
    """_process_chunk 的兄弟版本：失败时二分重试，成功时把 upsert 细节收集起来。"""
    if not chunk:
        return
    try:
        tasks = llm.extract_batch(chunk, backend=backend, timeout=timeout)
    except Exception as e:
        if len(chunk) > 1:
            mid = len(chunk) // 2
            _process_chunk_detail(conn, chunk[:mid], backend, timeout, result)
            _process_chunk_detail(conn, chunk[mid:], backend, timeout, result)
            return
        result["errors"].append(f"第 {chunk[0].get('id')} 条消息抽取失败：{str(e)[:200]}")
        return

    for t in tasks:
        result["raw"].append(dict(t))
        result["tasks"].append(db.upsert_task_detail(conn, t))
    db.mark_extracted(conn, [m["id"] for m in chunk])


def ingest_text(text: str, source: str = "manual", chat: str = "手动录入", sender: str | None = None) -> int:
    """把一整段文本（每行一条消息，或整块内容）灌进消息库。"""
    conn = db.connect()
    try:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        items = [
            {"source": source, "chat": chat, "sender": sender, "sent_at": db.now_iso()[:16], "content": ln}
            for ln in (lines or [text])
        ]
        return db.add_messages(conn, items)
    finally:
        conn.close()
