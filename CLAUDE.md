# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目

把微信群 / QQ 群 / 雨课堂 / 智慧树里的作业截止时间抽出来，汇总成网页看板 + 日历订阅 + Telegram 提醒。
纯 Python，无构建步骤，跑在 Windows 上（`.doc` 解析依赖本机 Word COM）。

**没有测试套件**，也没有 lint 配置。改完代码最低限度跑 `python -m compileall -q ddlhub`；
涉及抽取 / 提示词的改动要真调一次模型看输出（见下）。

## 常用命令

```bash
pip install -r requirements.txt

python run.py                      # 起看板(127.0.0.1:8848) + 每日定时采集/抽取/推送 + TG 录入长轮询
python run.py --once               # 立即抽取一次，终端打印摘要
python run.py --collect --once     # 先采集所有 enabled 数据源，再抽取
python run.py --once --push        # 抽取 + 推送到 Telegram

python -m ddlhub.setup_llm         # 模型配置向导（探测连通性后写回 config.yaml 的 llm 段）
python -m ddlhub.kinds             # 用确定性规则复查历史 kind，只打印
python -m ddlhub.kinds --apply     # 确认后落库

python -m compileall -q ddlhub     # 唯一的静态检查
```

验证抽取效果的唯一手段是真调模型：

```bash
python -c "from ddlhub import llm, pipeline; \
p=llm.build_provider(pipeline.load_config()['llm']); \
print(llm.extract_batch([{'source':'qq','chat':'x','sent_at':'2026-10-30 16:00','sender':'t','content':'第四章习题下周三前交'}], provider=p))"
```

`prompt_test.md` 是金标准用例集（9 条消息 + 期望 JSON + 评分标准），改 `SYSTEM_PROMPT` 后按它回归。
注意它**内嵌了一份提示词副本**，会随代码漂移——改了 `SYSTEM_PROMPT` 记得手动同步过去。

改完 `ddlhub/web/index.html` 想确认 JS 没写坏：把 `<script>` 内容抽出来交给 `node --check`。

## 数据流

```
sources/*.py collect()         采集器只往 messages 表塞原始文本，不做抽取
      ↓  messages (extracted=0)
pipeline.run_extraction()      按 batch_size 切块；整批 JSON 解析失败就二分重试
      ↓
llm.extract_batch()            组 prompt → 调 provider → 解析 JSON → 字段后处理
      ↓
db.upsert_task_detail()        指纹合并，返回 created / updated / unchanged
      ↓  tasks
web/ · ics.py · notify.py      看板、日历订阅、每日摘要
```

`run.py` 除了 uvicorn，还起两个后台线程：每日定时的 `_scheduler`、Telegram 录入的 `intake.start()`。

## 不读多个文件就会踩的约定

**时间一律 +08:00。** 项目时区是 `db.TZ`，模型输出、手输、解析、`ics` 全部按它解释，
`datetime.now()` 不带 tz 的写法一律是 bug。网页展示用 `Intl` 钉到 `Asia/Shanghai`，
否则浏览器不在 GMT+8 时「看板上的时间」和「编辑框里的时间」会打架。
`llm._clean_due` 还会丢掉 [当前年-2, 当前年+3] 之外的离谱年份。

**任务身份由 `db.fingerprint(title, course)` 决定**，两边都先归一（标题剥括号注释、
课程剥「2026秋」「…群」前后缀）。指纹没命中时还有两层兜底：标题包含式匹配
（`_same_task`），以及「同课程 + 同截止日 + 同组号（`_set_key`：第X章 / N.x）」合并。
后果：手动改课程名会让之后的抽取对不上而产生重复条目；改标题则不会覆盖已有任务的标题。

**`due_at` 的权威性归自动抽取**，但 `upsert_task_detail` 有两条保护：
`due is None` 不会抹掉已有时间（`keep_due`，手动补的日期不被后续重跑清掉）；
同一天内只差钟点（`00:00` vs `23:59`）用 `_same_day` 视为没改期，不算 fake「改期」。

**`kind` 不交给模型单方面决定。** 优先级在 `ddlhub/kinds.py`：采集器 hint
（`messages.kind_hint` 列，雨课堂 `type=19/20`、智慧树 `taskType` 写入）>
标题关键词规则表 > 模型（枚举内才认）> 「其他」。合并时 `kinds.prefer()`
挡住「已分好的类型退化成其他」。

**LLM 层是 provider 抽象。** 每家一个 `ddlhub/llm/providers/*.py`，统一
`complete(prompt, timeout)` / `complete_image(prompt, path, timeout)`；
`extract_batch` 只负责组 prompt 和解析结果，不关心是哪家。
`config.yaml` 的 `llm:` 段驱动 `build_provider()`，老配置的 `backend:` 由 `ALIASES` 兼容。

**所有出网请求走 `ddlhub/net.py`。** `net.request()` / `net.Session()` 统一处理指数退避
重试和日志里的密钥脱敏；新加 HTTP 调用别直接 `requests.get/post`。网络类失败要抛
`ProviderError(transient=True)`，`pipeline._process_chunk` 见到 `transient` 不会二分拆批
——否则一次断网会把 60 条消息放大成 60 次模型调用。重试参数在 `config.yaml` 的 `retry:` 段。

**附件解析是三层兜底**（`attach.py`）：本地解析（docx/pdf/pptx/xlsx/Word COM）→
本地结果不可用（空文本、乱码、扫描件）或格式不认识 → 远端解析，图片先试
`llm.vision_model`，失败退回本机 `claude -p`。解析出的 engine 会写进消息头，
排查「这条为什么没抽出来」时先看它。

**Telegram 录入复用提醒 bot 的 `getUpdates` 槽位**（那个 bot 平时只发消息不拉更新）。
所有命令必须在「录入会话」分支之前分发，否则 `/list` 会被当成作业内容吞掉。
offset 存 `data/tg_offset.json`，首次启动跳过历史积压。

**采集器统一签名** `collect(conn, cfg, sec) -> {"added": n, ...}`，在
`sources/__init__.py` 的 `COLLECTORS` 里注册；单个源抛异常不影响其他源。
采集器只入库，绝不直接写 `tasks`。同一份内容要能稳定去重 → `msg_uid` 里带上会让内容
「变了」的字段（雨课堂/智慧树把截止时间放进 uid，改期才会触发改期合并）。

**数据库迁移在 `db.connect()` 里做。** `CREATE TABLE IF NOT EXISTS` 不会给已存在的表加列，
新列要同时写进 `SCHEMA` 和 `_ADDED_COLUMNS`。

**网页改作业走 `PATCH /api/tasks/{id}`**：字段传 `null` 或空串表示清空，
可改字段由 `db.EDITABLE_FIELDS` / `CLEARABLE_FIELDS` 约束，改标题/课程会重算指纹，
撞名返回 409 而不是 500。

## 配置与敏感文件

`config.yaml`（含雨课堂/智慧树 Cookie、Telegram token、模型 key）和 `data/`（个人作业库、
下载的群文件）都在 `.gitignore` 里，**不要提交**；`config.yaml.bak` 同样含密钥，也已忽略。
`config.example.yaml` 是给外部看的模板，改配置项时要同步更新它和 README。
