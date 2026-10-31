"""智慧树 / 知到：抓课程和作业截止时间。

接口没有官方文档。接口地址、字段名和加密常量参考自
https://github.com/pjm314159/zhs （GPLv3，看的是 docs/spec.md 和 config.toml.example）
以及 https://github.com/VermiIIi0n/fuckZHS 的逆向笔记；本文件是独立实现，
没有复制它们的源码，只用了「哪个地址、发什么字段、用什么常量」这类事实信息。

三个要点：

1. 所有请求体先 `json.dumps` 再 AES-128-CBC/PKCS7 加密、base64，塞进 `secretStr`；
   不同接口用不同 key（home_key / ai_key …），见 ENDPOINTS。
2. 只要浏览器 Cookie，不需要账号密码。但 `studentexam-api` 那个域名额外需要 CAS 票：
   流程是 GET `passport/cas/login?service=<接口地址>` → 302 带 ticket → **用 POST** 打到带
   ticket 的地址完成验证。用 GET 会 401（接口是 POST-only），会话种不下来就一直 302。
   另外 Cookie 必须写进 cookie jar——设成固定请求头会盖掉 CAS 新种的 JSESSIONID。
3. 时间字段各接口不统一：AI 的 taskList 是 `endTime`（"2026-11-04 23:59:00"，无时区），
   共享课作业是 `endDate`。

已经验证可用的：
- AI 智课：queryStudentAICourseList（课程）→ taskList（作业，带 endTime）
- 共享课：queryShareCourseInfo（课程）→ getStudentHomework（作业，带 endDate）
- 未提交考试：getStudentNoCommitExam（当前为空，接口可用）
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .. import db

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)
LOOKBACK_DAYS = 7

# 客户端常量，站点改版就可能失效；都可以在 config 里覆盖
DEFAULT_IV = "1g3qqdh4jvbskb9x"
DEFAULT_KEYS = {
    "home": "7q9oko0vqb3la20r",
    "ai": "hw2fdlwcj4cs1mx7",
    "exam": "onbfhdyvz8x7otrp",
}

ONLINESERVICE = "https://onlineservice-api.zhihuishu.com"
KG_RUN = "https://kg-run-student.zhihuishu.com"
AI_WEB = "https://ai-smart-course-student-pro.zhihuishu.com"
STUDENT_EXAM = "https://studentexam-api.zhihuishu.com"
HOMEWORK_URL = f"{STUDENT_EXAM}/studentExam/gateway/t/v1/student/getStudentHomework"


class ZHSEncrypt:
    def __init__(self, iv: str, keys: dict):
        self.iv = iv.encode()
        self.keys = {k: v.encode() for k, v in keys.items()}

    def enc(self, payload: dict, key_name: str) -> str:
        key = self.keys.get(key_name)
        if not key or len(key) != 16 or len(self.iv) != 16:
            raise RuntimeError(
                f"智慧树的加密常量不对（key={key_name}）。站点改版了就去 "
                "config.yaml → sources.zhihuishu 里覆盖 exam_key / ai_key / iv"
            )
        data = json.dumps(payload, separators=(",", ":")).encode()
        pad = 16 - len(data) % 16
        data += bytes([pad]) * pad
        e = Cipher(algorithms.AES(key), modes.CBC(self.iv)).encryptor()
        return base64.b64encode(e.update(data) + e.finalize()).decode()


def _session(cookie: str) -> requests.Session:
    """Cookie 写进 cookie jar，不要设成请求头。

    studentexam-api 那条链路会通过 CAS 种新的 JSESSIONID；如果用一个固定的
    Cookie 请求头，requests 发送时会把它盖掉，会话就永远建不起来（一直 302）。
    """
    s = requests.Session()
    for part in cookie.split(";"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        s.cookies.set(k.strip(), v.strip(), domain=".zhihuishu.com")
    s.headers.update(
        {
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://onlineweb.zhihuishu.com/",
            "Origin": "https://onlineweb.zhihuishu.com",
        }
    )
    return s


def _cas_login(sess: requests.Session, service_url: str) -> bool:
    """给需要 CAS 会话的域名（studentexam-api）换一张票。

    流程：GET passport/cas/login?service=... → 302 带 ticket → POST 到该地址完成验证。
    注意必须用 POST——那个接口是 POST-only，用 GET 校验会 401，会话种不下来。
    """
    cas = "https://passport.zhihuishu.com/cas/login?service=" + requests.utils.quote(service_url, safe="")
    try:
        r = sess.get(cas, timeout=30, allow_redirects=False, headers={"Referer": "https://www.zhihuishu.com/"})
    except requests.RequestException as e:
        print(f"[zhihuishu] CAS 请求失败：{e}")
        return False
    loc = r.headers.get("Location", "")
    if r.status_code != 302 or "ticket=" not in loc:
        print("[zhihuishu] CAS 没拿到 ticket，Cookie 可能已过期")
        return False
    try:
        sess.post(loc, data={}, timeout=30, allow_redirects=False,
                  headers={"Referer": "https://studyh5.zhihuishu.com/"})
    except requests.RequestException:
        return False
    return True


def _post_form(sess, url, body, timeout=25, headers=None):
    r = sess.post(url, data=body, timeout=timeout, headers=headers or {})
    return _json(r)


def _post_json(sess, url, body, timeout=25):
    r = sess.post(
        url,
        json=body,
        timeout=timeout,
        headers={
            "Content-Type": "application/json;charset=UTF-8",
            "Referer": f"{AI_WEB}/",
            "Origin": AI_WEB,
        },
    )
    return _json(r)


def _get(sess, url, params, timeout=25):
    r = sess.get(url, params=params, timeout=timeout)
    return _json(r)


def _json(r):
    if r.status_code in (401, 403):
        raise RuntimeError("智慧树认证失败（cookie 过期），重新复制一次 Cookie")
    if r.status_code >= 400:
        raise RuntimeError(f"智慧树返回 HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError:
        raise RuntimeError("智慧树返回的不是 JSON，cookie 可能已失效") from None


def _check(data: dict, what: str) -> dict:
    """业务状态码藏在 body 里，HTTP 200 也可能是失败。"""
    if not isinstance(data, dict):
        raise RuntimeError(f"{what} 返回格式不对")
    code = data.get("code", data.get("status"))
    text = json.dumps(data, ensure_ascii=False)[:200]
    if "aes解密异常" in text:
        raise RuntimeError(f"{what} 解密失败：智慧树的加密常量变了，去 config 里覆盖")
    if str(code) not in ("200", "0", "None"):
        raise RuntimeError(f"{what} 失败：{data.get('message') or data.get('msg') or code}")
    return data


def _to_iso(value) -> str | None:
    """'2026-11-04 23:59:00' / 毫秒时间戳 → 带 +08:00 的 ISO。"""
    if not value:
        return None
    if isinstance(value, (int, float)) or str(value).isdigit():
        try:
            return datetime.fromtimestamp(int(value) / (1000 if int(value) > 1e11 else 1), db.TZ).isoformat(
                timespec="seconds"
            )
        except (ValueError, OSError, OverflowError):
            return None
    s = str(value).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=db.TZ)
            if fmt == "%Y-%m-%d":
                dt = dt.replace(hour=23, minute=59)
            return dt.isoformat(timespec="seconds")
        except ValueError:
            continue
    return None


def _human(iso: str | None) -> str:
    if not iso:
        return "未给时间"
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


def _share_courses(sess, zhs) -> list[dict]:
    """共享课。status 1 和 3 都会返回进行中的课，去重。"""
    seen, out = set(), []
    for status in (1, 3):
        body = {"secretStr": zhs.enc({"status": status, "pageNo": 1, "pageSize": 50}, "home")}
        data = _check(_post_form(sess, f"{ONLINESERVICE}/gateway/t/v1/student/course/share/queryShareCourseInfo", body), "共享课列表")
        for c in ((data.get("result") or {}).get("courseOpenDtos") or []):
            cid = c.get("courseId")
            if cid in seen:
                continue
            seen.add(cid)
            out.append(c)
    return out


def _ai_courses(sess, zhs) -> list[dict]:
    body = {"secretStr": zhs.enc({"status": 3}, "home")}
    data = _check(_post_form(sess, f"{ONLINESERVICE}/gateway/t/v1/student/queryStudentAICourseList", body), "AI 课程列表")
    return [c for c in (data.get("rt") or []) if isinstance(c, dict)]


def _ai_tasks(sess, zhs, course: dict) -> list[dict]:
    """AI 智课作业。taskType=1 是作业，status=0 表示未完成。"""
    payload = {
        "courseId": str(course.get("courseId")),
        "taskType": 1,
        "taskName": "",
        "status": 0,
    }
    body = {
        "secretStr": zhs.enc(payload, "ai"),
        "date": int(datetime.now(db.TZ).timestamp() * 1000),
    }
    data = _check(_post_json(sess, f"{KG_RUN}/student/gateway/t/task/taskList", body), "AI 课程作业")
    return [t for t in (data.get("data") or []) if isinstance(t, dict)]


def _share_homework(sess, zhs, course: dict) -> list[dict]:
    """共享课的作业列表。需要先完成 CAS 登录，否则 302。"""
    rid = course.get("recruitId")
    if not rid:
        return []
    body = {"secretStr": zhs.enc({"recruitId": rid}, "exam")}
    data = _check(
        _post_form(sess, HOMEWORK_URL, body, headers={"Referer": "https://studyh5.zhihuishu.com/"}),
        "共享课作业",
    )
    rt = data.get("rt") or {}
    for key in ("data", "homeworkList", "examList", "list"):
        items = rt.get(key)
        if isinstance(items, list):
            return items
    for value in rt.values():  # 兜底：取 rt 里第一个列表
        if isinstance(value, list):
            return value
    return []


def _no_commit_exam(sess, zhs) -> list[dict]:
    body = {"secretStr": zhs.enc({}, "home")}
    data = _check(_get(sess, f"{ONLINESERVICE}/gateway/t/v1/student/getStudentNoCommitExam", body), "未提交考试")
    items = ((data.get("rt") or {}).get("data")) or []
    return [t for t in items if isinstance(t, dict)]


def _entry(
    source_tag: str, course: str, title: str, due: str | None, uid_key: str,
    extra: str = "", kind: str | None = None,
) -> dict | None:
    if not title:
        return None
    if due:
        try:
            if datetime.fromisoformat(due) < datetime.now(db.TZ) - timedelta(days=LOOKBACK_DAYS):
                return None
        except ValueError:
            pass
    return {
        "source": "zhihuishu",
        "chat": course or "智慧树",
        "sender": "智慧树",
        "sent_at": db.now_iso()[:16],
        "content": f"【智慧树·{source_tag}】课程《{course}》作业「{title}」{extra}，截止时间 {_human(due)}",
        # 接口能确定的类型直接带上，省得模型乱猜
        "kind_hint": kind,
        # 截止时间进 uid：改期算作新消息，触发改期合并
        "msg_uid": f"zhs:{uid_key}:{due}",
    }


def collect(conn, cfg, sec) -> dict:
    cookie = (sec.get("cookie") or "").strip()
    if not cookie or cookie.startswith("<"):
        raise RuntimeError("还没填智慧树 Cookie：config.yaml → sources.zhihuishu.cookie")

    keys = dict(DEFAULT_KEYS)
    for name in ("home", "ai", "exam"):
        override = (sec.get(f"{name}_key") or "").strip()
        if override:
            keys[name] = override
    zhs = ZHSEncrypt((sec.get("iv") or DEFAULT_IV).strip(), keys)
    sess = _session(cookie)

    rows: list[dict] = []
    courses = 0

    # AI 智课作业（唯一确认带截止时间的来源）
    for course in _ai_courses(sess, zhs):
        courses += 1
        name = course.get("courseName") or str(course.get("courseId"))
        try:
            tasks = _ai_tasks(sess, zhs, course)
        except Exception as e:
            print(f"[zhihuishu] 跳过 AI 课程 {name}：{e}")
            continue
        for t in tasks:
            if t.get("taskType") not in (1, "1"):
                continue
            due = _to_iso(t.get("endTime"))
            row = _entry(
                "AI智课",
                name,
                t.get("taskName") or "",
                due,
                f"ai:{course.get('courseId')}:{t.get('examTestId') or t.get('id')}",
                f"（{t.get('startTime', '')[:10]} 布置）" if t.get("startTime") else "",
                kind="作业",
            )
            if row:
                rows.append(row)

    # 共享课作业：要先给 studentexam-api 换一张 CAS 票，否则一直是 302
    try:
        share = _share_courses(sess, zhs)
        courses += len(share)
        if share and _cas_login(sess, HOMEWORK_URL):
            for c in share:
                name = c.get("courseName") or str(c.get("courseId"))
                try:
                    items = _share_homework(sess, zhs, c)
                except Exception as e:
                    print(f"[zhihuishu] 跳过共享课 {name}：{e}")
                    continue
                for it in items:
                    # 1=未提交，4=已提交未达标可重做
                    if it.get("state") not in (1, 4, "1", "4"):
                        continue
                    due = _to_iso(it.get("endDate"))
                    row = _entry(
                        "共享课",
                        it.get("courseName") or name,
                        it.get("examName") or "",
                        due,
                        f"share:{c.get('courseId')}:{it.get('id') or it.get('examId')}",
                        f"（{it.get('cpOrderNumber')}）" if it.get("cpOrderNumber") else "",
                    )
                    if row:
                        rows.append(row)
    except Exception as e:
        print(f"[zhihuishu] 共享课作业拉取失败：{e}")

    # 未提交考试（接口可用，当前账号为空）
    try:
        for e in _no_commit_exam(sess, zhs):
            due = _to_iso(e.get("endTime") or e.get("endDate") or e.get("examEndTime"))
            row = _entry(
                "考试",
                e.get("courseName") or "",
                e.get("examName") or e.get("name") or "",
                due,
                f"exam:{e.get('examId') or e.get('id')}",
                kind="考试",
            )
            if row:
                rows.append(row)
    except Exception as e:
        print(f"[zhihuishu] 未提交考试拉取失败：{e}")

    return {"added": db.add_messages(conn, rows), "courses": courses, "items": len(rows)}
