"""作业性质（kind）分类。

以前 kind 完全由模型自由发挥，同一个模型跑两次可能给出不同的值，弱模型还会
把「小测」写成「考试」。这里把能确定的部分收回来：

  1. 采集器本来就知道的（雨课堂 type=19/20、智慧树 taskType / 未提交考试）→ hint
  2. 标题关键词 → 规则表（顺序即优先级，「小组作业」不会被「作业」抢走）
  3. 模型给的，但必须在枚举内
  4. 兜底「其他」

三个来源的优先级就是上面这个顺序，`resolve_kind()` 负责合成。
"""

from __future__ import annotations

import re

KIND_CHOICES = [
    "作业",
    "小测",
    "考试",
    "实验报告",
    "论文",
    "展示",
    "阅读",
    "编程",
    "小组项目",
    "其他",
]

FALLBACK = "其他"

EMOJI = {
    "作业": "📝",
    "小测": "⏱",
    "考试": "📕",
    "实验报告": "🔬",
    "论文": "📄",
    "展示": "🎤",
    "阅读": "📖",
    "编程": "💻",
    "小组项目": "👥",
    "其他": "•",
}

# 老数据/手输的各种写法都归到正名。「实验」是上一版枚举留下的。
ALIASES = {
    "实验": "实验报告",
    "实验课": "实验报告",
    "lab": "实验报告",
    "测验": "小测",
    "随堂测": "小测",
    "堂测": "小测",
    "quiz": "小测",
    "exam": "考试",
    "论文写作": "论文",
    "essay": "论文",
    "presentation": "展示",
    "reading": "阅读",
    "homework": "作业",
    "assignment": "作业",
    "coding": "编程",
    "code": "编程",
    "项目": "小组项目",
    "project": "小组项目",
    "其它": "其他",
    "other": "其他",
}

# 「XX 作业」比「小组作业」泛，所以具体的排前面，先命中先返回。
_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("小组项目", ("小组项目", "小组作业", "团队项目", "团队作业", "课程项目", "project", "group work")),
    ("论文", ("论文", "thesis", "essay", "dissertation", "paper")),
    ("展示", ("展示", "答辩", "汇报", "演示", "演讲", "presentation", "demo")),
    ("实验报告", ("实验报告", "上机实验", "实验", "lab report", "lab")),
    ("阅读", ("阅读", "读书报告", "读书", "reading", "read")),
    ("编程", ("编程", "代码", "程序设计", "coding", "code", "programming")),
    ("小测", ("小测", "测验", "随堂测", "堂测", "单元测试", "quiz")),
    ("考试", ("考试", "期末考", "期中考", "exam")),
    ("作业", ("作业", "习题", "练习", "homework", "assignment", "翻译", "translation")),
)

_ASCII_RE = re.compile(r"^[\x20-\x7e]+$")


def _compile(tokens: tuple[str, ...]) -> tuple:
    """英文词按整词匹配，免得 `lab` 命中 `label`、`code` 命中 `encoded`。"""
    return tuple(
        re.compile(rf"\b{re.escape(t)}\b") if _ASCII_RE.match(t) else t
        for t in tokens
    )


_COMPILED: tuple[tuple[str, tuple], ...] = tuple((k, _compile(t)) for k, t in _RULES)


def normalize(value) -> str | None:
    """把任意写法收敛到枚举内；认不出来返回 None。"""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s in KIND_CHOICES:
        return s
    return ALIASES.get(s.lower())


def guess_from_title(title: str | None) -> str | None:
    """按标题关键词猜类型，猜不出返回 None。"""
    if not title:
        return None
    low = str(title).lower()
    for kind, pats in _COMPILED:
        for p in pats:
            if p.search(low) if hasattr(p, "search") else p in low:
                return kind
    return None


def prefer(old, new) -> str | None:
    """合并时用：新一版结果只有不比原来更含糊时才采纳。

    模型偶尔会把已经分好的类型退化成兜底值「其他」，那种情况保留原来的，
    否则每重跑一次分类就抖一次。
    """
    new_n = normalize(new)
    old_n = normalize(old)
    if not new_n:
        return old_n
    if new_n == FALLBACK and old_n and old_n != FALLBACK:
        return old_n
    return new_n


def resolve_kind(title: str | None, llm_kind=None, hint=None) -> str:
    """确定性来源 > 标题关键词 > 模型 > 其他。"""
    h = normalize(hint)
    if h:
        return h
    g = guess_from_title(title)
    if g:
        return g
    n = normalize(llm_kind)
    return n or FALLBACK


def main(argv: list[str] | None = None) -> int:
    """`python -m ddlhub.kinds [--apply]`：用确定性规则复查库里的历史任务。

    默认只打印会改哪些，加 --apply 才落库。
    """
    import sys

    argv = list(sys.argv[1:] if argv is None else argv)
    apply = "--apply" in argv

    from . import db

    conn = db.connect()
    try:
        rows = conn.execute("SELECT id, title, kind FROM tasks ORDER BY id").fetchall()
        changes = []
        for r in rows:
            old = normalize(r["kind"]) or r["kind"] or ""
            guess = guess_from_title(r["title"] or "")
            if guess and guess != old:
                changes.append((r["id"], r["title"], old, guess))

        print(f"检查 {len(rows)} 条，需要改 {len(changes)} 条")
        for tid, title, old, new in changes:
            print(f"  #{tid:<4} {title[:34]:<36} {old or '(空)':<6} → {new}")

        if changes and apply:
            ts = db.now_iso()
            for tid, _, _, new in changes:
                conn.execute("UPDATE tasks SET kind = ?, updated_at = ? WHERE id = ?", (new, ts, tid))
            conn.commit()
            print(f"已写入 {len(changes)} 条")
        elif changes:
            print("（没落库，加 --apply 才写入）")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
