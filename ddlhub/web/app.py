"""网页看板 + 接口。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from .. import db, ics, notify, pipeline

WEB_DIR = Path(__file__).resolve().parent
app = FastAPI(title="作业 DDL 汇总", docs_url=None, redoc_url=None)


class IngestIn(BaseModel):
    text: str
    chat: str = "手动录入"
    source: str = "manual"


class StatusIn(BaseModel):
    status: str


class PatchIn(BaseModel):
    title: str | None = None
    course: str | None = None
    kind: str | None = None
    due_at: str | None = None
    due_text: str | None = None


@app.get("/", response_class=HTMLResponse)
def index():
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/api/tasks")
def api_tasks(status: str = Query("open")):
    conn = db.connect()
    try:
        return {"tasks": db.list_tasks(conn, status), "now": db.now_iso()}
    finally:
        conn.close()


@app.post("/api/tasks/{task_id}/status")
def api_set_status(task_id: int, body: StatusIn):
    if body.status not in {"open", "done", "ignored"}:
        raise HTTPException(400, "status 只能是 open/done/ignored")
    conn = db.connect()
    try:
        if not db.set_status(conn, task_id, body.status):
            raise HTTPException(404, "任务不存在")
        return {"ok": True}
    finally:
        conn.close()


@app.patch("/api/tasks/{task_id}")
def api_patch(task_id: int, body: PatchIn):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    conn = db.connect()
    try:
        if not db.update_task(conn, task_id, fields):
            raise HTTPException(404, "任务不存在或没有可改字段")
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/ingest")
def api_ingest(body: IngestIn):
    n = pipeline.ingest_text(body.text, source=body.source, chat=body.chat)
    return {"ok": True, "added": n}


@app.post("/api/extract")
def api_extract():
    stats = pipeline.run_extraction()
    return {"ok": not stats["errors"], **stats}


@app.get("/api/digest")
def api_digest():
    cfg = pipeline.load_config()
    return {"text": notify.digest(cfg=cfg, hours=int(cfg.get("notify", {}).get("digest_hours", 48)))}


@app.get("/api/stats")
def api_stats():
    conn = db.connect()
    try:
        tasks = db.list_tasks(conn, "open")
        msg = conn.execute("SELECT COUNT(*) c, SUM(extracted=0) p FROM messages").fetchone()
        run = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()

    now = datetime.now(db.TZ)
    overdue = today = week = undated = 0
    for t in tasks:
        if not t.get("due_at"):
            undated += 1
            continue
        h = (datetime.fromisoformat(t["due_at"]) - now).total_seconds() / 3600
        if h < 0:
            overdue += 1
        elif h <= 24:
            today += 1
        if 0 <= h <= 168:
            week += 1
    return {
        "open": len(tasks),
        "overdue": overdue,
        "today": today,
        "week": week,
        "undated": undated,
        "messages": msg["c"] or 0,
        "pending": msg["p"] or 0,
        "last_run": dict(run) if run else None,
    }


@app.get("/calendar.ics")
def api_ics(request: Request):
    conn = db.connect()
    try:
        tasks = db.list_tasks(conn, "open")
    finally:
        conn.close()
    text = ics.build_ics([t for t in tasks if t.get("due_at")])
    return Response(
        content=text.encode("utf-8"),
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": 'inline; filename="ddl.ics"'},
    )
