"""数据源采集器。

每个模块暴露 `collect(conn, cfg, sec) -> {"added": n}`，由 collect_all 按
config.yaml 里的 sources.<name>.enabled 调度。采集器只负责往 messages 表塞原始
文本，抽取交给 LLM 流水线。
"""

from __future__ import annotations

from . import qq, wechat, yuketang

COLLECTORS = {
    "yuketang": yuketang.collect,
    "qq": qq.collect,
    "wechat": wechat.collect,
}


def collect_all(conn, cfg: dict) -> dict:
    out: dict[str, dict] = {}
    sources_cfg = (cfg or {}).get("sources") or {}
    for name, fn in COLLECTORS.items():
        sec = sources_cfg.get(name) or {}
        if not sec.get("enabled"):
            continue
        try:
            res = fn(conn, cfg, sec) or {}
            out[name] = {"added": int(res.get("added", 0)), **{k: v for k, v in res.items() if k != "added"}}
        except Exception as e:  # 单个源失败不影响其他源
            out[name] = {"added": 0, "error": f"{type(e).__name__}: {e}"[:400]}
    return out
