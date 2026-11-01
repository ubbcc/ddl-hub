"""把消息跑过 LLM，落到任务表里。"""

from __future__ import annotations

from pathlib import Path

import yaml

from . import db, llm, net


def load_config(path: Path | str | None = None) -> dict:
    p = Path(path) if path else Path(__file__).resolve().parent.parent / "config.yaml"
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class _TransientAbort(Exception):
    """网络故障：本轮扫描别继续了，剩下的消息留待下次运行。"""


def _provider(cfg: dict, stats: dict):
    """配置有问题时把原因写进 stats["errors"]，返回 None 让调用方收工。"""
    try:
        return llm.build_provider(cfg.get("llm") or {}, retry=net.settings(cfg, "llm"))
    except llm.ProviderError as e:
        stats["errors"].append(f"模型配置有问题：{e}")
        return None


def run_extraction(conn=None, config: dict | None = None, limit: int = 500) -> dict:
    cfg = config or load_config()
    llm_cfg = cfg.get("llm", {})
    timeout = int(llm_cfg.get("timeout_seconds", 240))
    batch_size = max(1, int(llm_cfg.get("batch_size", 60)))

    own = conn is None
    conn = conn or db.connect()
    started = db.now_iso()
    stats = {"messages": 0, "created": 0, "updated": 0, "unchanged": 0, "errors": []}
    try:
        provider = _provider(cfg, stats)
        if provider is None:
            db.log_run(conn, started, False, str(stats["errors"]))
            return stats

        rows = db.pending_messages(conn, limit=limit)
        if not rows:
            db.log_run(conn, started, True, "没有待处理消息")
            return stats

        msgs = [dict(r) for r in rows]
        stats["messages"] = len(msgs)

        for start in range(0, len(msgs), batch_size):
            chunk = msgs[start : start + batch_size]
            try:
                _process_chunk(conn, chunk, provider, timeout, stats)
            except _TransientAbort as e:
                stats["errors"].append(f"网络故障，本次扫描提前结束，剩下的消息留待下次：{str(e)[:200]}")
                break

        db.log_run(conn, started, not stats["errors"], str(stats))
        return stats
    finally:
        if own:
            conn.close()


def _process_chunk(conn, chunk: list[dict], provider, timeout: int, stats: dict) -> None:
    """整批失败就二分重试——模型偶尔会返回坏 JSON，把坏样本隔离出来，
    避免一批 60 条里有一条脏数据就全批丢弃。

    网络/限流类故障不二分：重试已经在 net.py 里做过了，再拆批只会把一次失败
    放大成 N 次调用，整批留给下次运行即可。"""
    if not chunk:
        return
    try:
        tasks = llm.extract_batch(chunk, provider=provider, timeout=timeout)
    except Exception as e:
        if getattr(e, "transient", False):
            raise _TransientAbort(str(e)) from None
        if len(chunk) > 1:
            mid = len(chunk) // 2
            _process_chunk(conn, chunk[:mid], provider, timeout, stats)
            _process_chunk(conn, chunk[mid:], provider, timeout, stats)
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
    timeout = int(llm_cfg.get("timeout_seconds", 240))
    batch_size = max(1, int(llm_cfg.get("batch_size", 60)))

    rows = [r for r in rows if isinstance(r, dict) and r.get("id") is not None]
    result: dict = {"messages": len(rows), "raw": [], "tasks": [], "errors": []}
    if not rows:
        return result

    provider = _provider(cfg, result)
    if provider is None:
        return result

    started = db.now_iso()
    for start in range(0, len(rows), batch_size):
        chunk = rows[start : start + batch_size]
        try:
            _process_chunk_detail(conn, chunk, provider, timeout, result)
        except _TransientAbort as e:
            result["errors"].append(f"网络故障，本次解析提前结束，剩下的留待下次：{str(e)[:200]}")
            break

    db.log_run(conn, started, not result["errors"], f"manual: {result['tasks']}")
    return result


def _process_chunk_detail(conn, chunk: list[dict], provider, timeout: int, result: dict) -> None:
    """_process_chunk 的兄弟版本：失败时二分重试，成功时把 upsert 细节收集起来。"""
    if not chunk:
        return
    try:
        tasks = llm.extract_batch(chunk, provider=provider, timeout=timeout)
    except Exception as e:
        if getattr(e, "transient", False):
            raise _TransientAbort(str(e)) from None
        if len(chunk) > 1:
            mid = len(chunk) // 2
            _process_chunk_detail(conn, chunk[:mid], provider, timeout, result)
            _process_chunk_detail(conn, chunk[mid:], provider, timeout, result)
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
