"""入口：起网页看板 + 每日定时采集/抽取/推送。

python run.py                     起服务
python run.py --once              立即抽取一次并打印摘要
python run.py --collect --once    先采集所有已启用的数据源，再抽取
python run.py --once --push       抽取 + 推到 Telegram
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ddlhub import db, intake, notify, pipeline, sources  # noqa: E402


def collect_sources(cfg: dict) -> dict:
    conn = db.connect()
    try:
        report = sources.collect_all(conn, cfg)
    finally:
        conn.close()
    for name, res in report.items():
        if "error" in res:
            print(f"[collect:{name}][error] {res['error']}", file=sys.stderr)
        elif res["added"]:
            print(f"[collect:{name}] 新增 {res['added']} 条消息")
    return report


def run_once(push: bool = False, collect: bool = False) -> None:
    cfg = pipeline.load_config()
    if collect:
        collect_sources(cfg)
    stats = pipeline.run_extraction(config=cfg)
    print(f"[extract] 扫描 {stats['messages']} 条消息 → 新增 {stats['created']}，"
          f"改期 {stats['updated']}，重复 {stats['unchanged']}")
    for e in stats["errors"]:
        print(f"[extract][error] {e}", file=sys.stderr)

    hours = int(cfg.get("notify", {}).get("digest_hours", 72))
    text = notify.digest(cfg=cfg, hours=hours)
    print()
    print(text)
    if push:
        notify.send(text, cfg)
        print("\n[push] 已发送到 Telegram")


def _scheduler(cfg: dict) -> None:
    hhmm = str(cfg.get("notify", {}).get("daily_time", "08:00"))
    try:
        hh, mm = (int(x) for x in hhmm.split(":"))
    except ValueError:
        hh, mm = 8, 0
    done_for = ""
    while True:
        now = datetime.now(db.TZ)
        today = now.strftime("%Y-%m-%d")
        # 过点就算：8 点没开机、9 点才启动当天也会补发一次，不至于漏掉
        past_due = now.hour > hh or (now.hour == hh and now.minute >= mm)
        if past_due and done_for != today:
            done_for = today
            try:
                collect_sources(cfg)
                stats = pipeline.run_extraction(config=cfg)
                text = notify.digest(cfg=cfg, hours=int(cfg.get("notify", {}).get("digest_hours", 72)))
                notify.send(text, cfg)
                print(f"[scheduler] {today} 完成：{stats}", flush=True)
            except Exception as e:
                print(f"[scheduler] 失败：{e}", file=sys.stderr, flush=True)
        time.sleep(30)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="只跑一次抽取，不起服务")
    ap.add_argument("--collect", action="store_true", help="先采集已启用的数据源，再抽取")
    ap.add_argument("--push", action="store_true", help="配合 --once，推送到 Telegram")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    args = ap.parse_args()

    cfg = pipeline.load_config()
    db.connect().close()

    if args.once:
        run_once(push=args.push, collect=args.collect)
        return

    import uvicorn

    from ddlhub.web.app import app

    threading.Thread(target=_scheduler, args=(cfg,), daemon=True).start()
    intake.start(cfg)

    host = args.host or cfg.get("server", {}).get("host", "127.0.0.1")
    port = args.port or int(cfg.get("server", {}).get("port", 8848))
    print(f"看板: http://{host}:{port}    日历: http://{host}:{port}/calendar.ics")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
