"""带退避重试的 HTTP 请求。

集中处理三件事：
  1. 临时网络错误（连接被掐、超时、5xx、429）按指数退避重试；
  2. 重试日志统一打在这里，URL 里的 bot token 会被打码；
  3. 重试耗尽后由调用方决定怎么处理（返回最后一个响应，或抛最后一个异常）。

只依赖 requests + 标准库，不 import ddlhub 内部模块，避免循环导入。
"""

from __future__ import annotations

import random
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable

import requests

DEFAULTS = {
    "enabled": True,
    "max_attempts": 3,  # 总尝试次数（含第一次）
    "initial_delay": 1.0,  # 首次退避秒数，之后 ×multiplier
    "max_delay": 20.0,  # 单次退避上限
    "multiplier": 2.0,
    "jitter": 0.3,  # ±30% 抖动，避免多路同时重试
    "retry_statuses": (408, 429, 500, 502, 503, 504),
    "honor_retry_after": True,
    "max_retry_after": 60.0,  # Retry-After 超过这个值就不干等
}

_TOKEN_RE = re.compile(r"bot(\d+):[\w-]+")
_SECRET_RE = re.compile(r"(?i)\b(api[_-]?key|token|secretstr)=([^&\s\"']{6,})")


def redact(text: object) -> str:
    """异常消息里经常带着整条 URL 和查询参数，别让密钥进日志。"""
    s = _TOKEN_RE.sub(r"bot\1:***", str(text))
    return _SECRET_RE.sub(lambda m: f"{m.group(1)}=***", s)


def _num(value, default: float, minimum: float) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    if n != n or n < minimum:  # NaN 或越界
        return default
    return n


def _statuses(value) -> tuple[int, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple, set)):
        return DEFAULTS["retry_statuses"]
    try:
        return tuple(int(x) for x in value)
    except (TypeError, ValueError):
        return DEFAULTS["retry_statuses"]


def normalize(retry: dict | None) -> dict:
    """把 retry 原始字典收敛成完整可用的参数，非法值回落到默认。"""
    raw = dict(retry) if isinstance(retry, dict) else {}
    out = dict(DEFAULTS)
    out.update({k: raw[k] for k in DEFAULTS if k in raw})
    out["enabled"] = bool(out["enabled"])
    out["max_attempts"] = max(1, int(_num(out["max_attempts"], DEFAULTS["max_attempts"], 1)))
    out["initial_delay"] = _num(out["initial_delay"], DEFAULTS["initial_delay"], 0.0)
    out["max_delay"] = max(
        out["initial_delay"], _num(out["max_delay"], DEFAULTS["max_delay"], 0.0)
    )
    out["multiplier"] = max(1.0, _num(out["multiplier"], DEFAULTS["multiplier"], 1.0))
    out["jitter"] = min(1.0, _num(out["jitter"], DEFAULTS["jitter"], 0.0))
    out["retry_statuses"] = _statuses(out["retry_statuses"])
    out["honor_retry_after"] = bool(out["honor_retry_after"])
    out["max_retry_after"] = _num(
        out["max_retry_after"], DEFAULTS["max_retry_after"], 0.0
    )
    return out


def settings(cfg: dict | None, area: str | None = None) -> dict:
    """从 config.yaml 的 retry 段取参数；缺省全部回落 DEFAULTS。

    cfg 传 None、没有 retry 段、段里缺键都安全。area 用于按子系统覆盖，
    例如 retry.llm.max_attempts。
    """
    raw = (cfg or {}).get("retry")
    if not isinstance(raw, dict):
        return normalize(None)
    merged = {k: raw[k] for k in DEFAULTS if k in raw}
    if area:
        sub = raw.get(area)
        if isinstance(sub, dict):
            merged.update({k: sub[k] for k in DEFAULTS if k in sub})
    return normalize(merged)


def delay_for(attempt: int, retry: dict | None = None) -> float:
    """第 attempt 次失败后的等待秒数（attempt 从 1 开始）。"""
    r = normalize(retry)
    base = min(
        r["initial_delay"] * (r["multiplier"] ** (max(1, attempt) - 1)),
        r["max_delay"],
    )
    if r["jitter"]:
        base *= 1.0 + random.uniform(-r["jitter"], r["jitter"])
    return max(0.0, base)


def _retry_after(
    resp: requests.Response,
    r: dict,
    retry_after_of: Callable[[requests.Response], float | None] | None,
) -> float | None:
    if not r["honor_retry_after"]:
        return None
    secs: float | None = None
    if retry_after_of is not None:
        try:
            secs = retry_after_of(resp)
        except Exception:
            secs = None
    if secs is None:
        header = (resp.headers or {}).get("Retry-After")
        if header:
            header = header.strip()
            try:
                secs = float(header)
            except ValueError:
                try:
                    dt = parsedate_to_datetime(header)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    secs = (dt - datetime.now(timezone.utc)).total_seconds()
                except (TypeError, ValueError):
                    secs = None
    if secs is None:
        return None
    return max(0.0, float(secs))


def _retryable_exc(exc: requests.RequestException, idempotent: bool, retry_timeouts) -> bool:
    # 连接都没建立起来的失败，无论幂等与否都可以安全重试
    if isinstance(
        exc, (requests.ConnectTimeout, requests.exceptions.SSLError, requests.exceptions.ProxyError)
    ):
        return True
    if retry_timeouts is None:
        retry_timeouts = idempotent
    if retry_timeouts:
        return isinstance(
            exc,
            (
                requests.Timeout,
                requests.ConnectionError,
                requests.exceptions.ChunkedEncodingError,
                requests.exceptions.ContentDecodingError,
            ),
        )
    # 非幂等请求：只认「连接没建立」这一类，不重试读超时/传输中断
    if isinstance(exc, requests.ConnectionError):
        text = str(exc)
        return any(
            key in text
            for key in ("NewConnectionError", "Failed to establish a new connection", "Connection refused")
        )
    return False


def request(
    method: str,
    url: str,
    *,
    timeout: float = 30,
    retry: dict | None = None,
    session: requests.Session | None = None,
    idempotent: bool = True,
    retry_timeouts: bool | None = None,
    label: str = "",
    retry_after_of: Callable[[requests.Response], float | None] | None = None,
    _send: Callable | None = None,
    **kwargs,
) -> requests.Response:
    """发请求并按需重试。

    idempotent=True（GET / 只读查询）连读超时一起重试；False（LLM、Telegram 发消息）
    只重试连接阶段失败和 429/5xx，避免服务端已处理的请求被重复提交。
    retry_timeouts 可以单独覆盖这个判断（notify 用它开读超时重试）。

    状态码重试耗尽后返回最后一个 Response，让调用方原有的 raise_for_status()
    和 401/403 判断继续生效；传输异常耗尽则重抛最后一个异常。
    """
    r = normalize(retry)
    attempts = r["max_attempts"] if r["enabled"] else 1
    send = _send or (session.request if session is not None else requests.request)
    tag = label or method

    for attempt in range(1, attempts + 1):
        try:
            resp = send(method, url, timeout=timeout, **kwargs)
        except requests.RequestException as e:
            if attempt >= attempts or not _retryable_exc(e, idempotent, retry_timeouts):
                raise
            wait = delay_for(attempt, r)
            print(
                f"[retry] {tag} 第 {attempt}/{attempts} 次失败（{redact(e)}），{wait:.1f}s 后重试",
                flush=True,
            )
            time.sleep(wait)
            continue

        if resp.status_code in r["retry_statuses"] and attempt < attempts:
            wait = _retry_after(resp, r, retry_after_of)
            if wait is not None and wait > r["max_retry_after"]:
                print(
                    f"[retry] {tag} 服务端要求等待 {wait:.0f}s（超过上限"
                    f" {r['max_retry_after']:.0f}s），直接放弃",
                    flush=True,
                )
                return resp
            if wait is None:
                wait = delay_for(attempt, r)
            print(
                f"[retry] {tag} 第 {attempt}/{attempts} 次失败（HTTP {resp.status_code}），"
                f"{wait:.1f}s 后重试",
                flush=True,
            )
            time.sleep(wait)
            continue

        if attempt > 1:
            print(f"[retry] {tag} 已恢复（第 {attempt} 次尝试成功）", flush=True)
        return resp

    raise requests.RequestException(f"{tag} 重试逻辑异常：没有执行任何请求")


class Session(requests.Session):
    """带重试的 requests.Session。

    采集器里原来的 sess.get/post 会自动走重试，不用逐处改参数；重试复用同一个
    会话，Cookie 和 CAS 种下的 JSESSIONID 都不会丢。
    """

    def __init__(self, retry: dict | None = None, label: str = ""):
        super().__init__()
        self.retry = normalize(retry)
        self.label = label

    def request(self, method: str, url: str, **kwargs):
        return request(
            method, url, retry=self.retry, label=self.label, _send=super().request, **kwargs
        )
