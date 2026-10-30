# TODO

当前状态：雨课堂采集已跑通并入库（20 条任务）；QQ 群消息**和群文件**均已用真实数据跑通（2026-10-30）；
微信采集器代码已写但从未用真实数据验证过，`enabled: false`。

## 1. QQ 群消息 + 群文件解析

- [x] NapCat 升级到 **v4.18.41**，解压在 `Downloads/NapCat-Framework-4.18.41`（旧版 QCE-v6.3.2 已移入回收站）
- [x] onebot11 配置已加 HTTP 服务：`127.0.0.1:3000`，`messagePostFormat: array`
- [x] `config.yaml` 里 `sources.qq.enabled: true`
- [x] 启动 `napiLoader.bat` 并登录 QQ（QQ 主号 2307886049）
- [x] `groups` 填 `2026秋电工学`（group_id 856585727）
- [x] 文字消息已入库（98 条历史 → 65 条新入库）
- [x] **群文件已跑通**：7 个文件全部解析入库，0 条「未解析」占位
  - 3 个 pptx → python-pptx
  - 2 张 jpg → claude-remote（OCR）
  - 1 个 作业.doc → word-com
  - 1 个 第6章.pptx
- [x] LLM 抽取：从群文件内容里抽到「第五章作业 5.2(10)-(12)、5.4-5.6 · 电工学」〔11-09〕
- [x] 重复下载保护：同名文件解析过就跳过，不再每轮重跑 OCR（`db.has_file_message`）
- [x] 解析成功后自动清理同名「未解析」占位行（`db.drop_placeholder`）
- [ ] 风控观察：主号挂一周看是否被限

### 群文件阻塞原因（已解决，2026-10-30）

NapCat v4.18.28 的 `packetBackend` 不支持 QQ 构建 `9.9.36-53644-x64`
（兼容表只到 36580），`get_group_file_url` 直接报错。NapCat **v4.18.34** 起才加入
该构建的 Packet 映射。升到 v4.18.41 后 `get_group_file_url` 正常返回直链。
降级 QQ 到 37625 不可行（低于最低要求 40768）。

### 已覆盖的版本差异

`message` 既支持 array 也支持 CQ 码字符串；file 段字段名兼容
`file`/`name`/`file_name` 和 `file_id`/`fid`/`id`，带 `busid` 时一并传给
`get_group_file_url`；`file` 字段是直链时自动取出并 URL 解码；下载或解析失败时
仍入库一条「群文件 XXX（未解析）」，保证 LLM 知道群里发过文件；连不上 NapCat 会
给明确报错而不是抛 `ConnectionError`。

### 分类准确性实测（2026-10-30，真实 68 条 + 7 条对抗用例）

真实群里三类内容混在一起，实测结果：

| 内容类型 | 条数 | 应抽 | 实抽 | 结论 |
|---|---|---|---|---|
| 闲聊（调课、书错、带电脑） | ~15 | 0 | 0 | ✅ |
| 习题讲解（10-28 暂态点评、10-29 三相点评） | ~40 | 0 | 0 | ✅ |
| 作业布置 / 作业清单表 | 2 处 | 多条 | 见下 | ⚠️ 已修 |

已修的两个漏抽/重复问题：

- **表格型文件只抽最后一行**：`作业.doc` 是「布置日 / 题目 / 提交日」三列 5 行，
  Word 解析把表格拍平成纯文本后，模型只取最后一行。已加提示词规则 10，要求逐行输出。
  现在 5 行全部抽出。
- **同一作业两个标题**：「第五章作业 5.2(10)-(12)、5.4-5.6」与稍后重抽出的
  「5.x 作业」指纹对不上，落了两条。已在 `upsert_task` 加兜底：同课程 + 同截止日 +
  同一组号（`第X章` 或 `N.x` 前缀归一）且类别词（作业/实验/报告）一致 → 视为同一条。

对抗测试（7 条混排：3 条习题讲解 + 1 条调课通知 + 1 条真作业 + 1 条闲聊 + 1 条点评）：
只抽出那条真作业，截止日推算正确（「周五之前」→ 当天）。

已知残留误报：「没拿第二次作业的同学下周一上午3-4节来第一排找」被抽成作业 DDL
（confidence 0.45）。语义上这是**领作业**不是**交作业**，属于误报，但信息本身有用。

待定：表格里 3 条已过期的历史作业（1.x / 3.x / 4.x）也会入库并显示在「已逾期」，
是否要过滤掉过期项需要用户确认。

风险点：`parseMultMsg` 仍是 false，老师用「合并转发」发作业时整条消息会被跳过。
真实数据跑通后再决定要不要打开，打开需要给 `_text_of` 加嵌套 node 的处理。

## 2. 微信群消息 + 群文件解析

- [ ] 微信降级到 3.9.12（当前是 4.1.15.13，wxauto 不支持 4.x）
- [ ] `sources.wechat.enabled: true`，`mode: wxauto`，`groups` 填群名
- [ ] 验证 `GetNextNewMessage()` 返回结构，确认群名与 `groups` 完全一致
- [ ] 验证文件消息：`m.type == 'file'` 时 `m.content` 是不是纯文件名
- [ ] 确认 `_find_local_file()` 能在 `Documents/WeChat Files` / `Documents/xwechat_files` 里找到对应文件
- [ ] 找不到的降级为「群文件 XXX（未解析）」+ 人工补录，确认这条路径不报错

风险点：wxauto 各版本 API 差异大；群文件常常只在手机上下载过、PC 端没有本地副本，需要用户先在 PC 上打开一次。

## 3. 作业来源可点击跳转

- [ ] `tasks` 表加 `url` 字段（`ALTER TABLE`，老库要迁移）
- [ ] 采集器填 url：
  - 雨课堂：`learn/` 活动里有 `classroom_id` + `leaf_id`，需要抓一次网页里作业详情页的真实 URL 格式（同上次抓 API 的办法）
  - QQ / 微信：有 http 链接的消息直接用那条链接
- [ ] `upsert_task` 写入 url；同一任务改期时保留原 url
- [ ] 看板卡片标题渲染成 `<a target="_blank">`，没有 url 的保持纯文本
- [ ] `.ics` 的 DESCRIPTION 里也带上链接

## 4. 完善作业性质分类

现状：`kind` 由 LLM 自由给出，取值 作业/考试/实验/论文/展示/其他，很不稳定。

- [ ] 扩充枚举：作业 / 小测 / 考试 / 实验报告 / 论文 / 展示 / 阅读 / 编程 / 小组项目 / 其他
- [ ] 能确定的来源优先用确定性规则，不要交给 LLM：
  - 雨课堂 `type` 19 → 作业，20 → 小测（`_label()` 已有，但目前只是拼进正文）
  - 标题含「实验报告」「论文」「展示」「reading」等关键词兜底
- [ ] 采集器把确定性 kind 直接传给抽取层，LLM 只负责它判不了的部分
- [ ] 看板按 kind 加颜色/图标区分
- [ ] 统计口径：按 kind 分组计数（哪类作业最多、哪门课最压人）

## 5. LLM 层大改：多 API 支持 + 免费模型配置向导

现状：`ddlhub/llm.py` 只有两个 backend（`claude-cli` / `api`）。`api` 写死了 Anthropic Messages 接口，模型名从环境变量 `DDLHUB_MODEL` 取，每加一家服务商都要改代码。

- [ ] 抽 provider 层：`ddlhub/llm/providers/` 每家一个模块，统一 `complete(prompt) -> str`，`extract_batch` 只负责组 prompt + 解析结果
- [ ] 优先做 OpenAI 兼容协议，一个实现覆盖大多数服务商：DeepSeek / 智谱 GLM / 硅基流动 / 月之暗面 / 通义 / 本地 Ollama、vLLM、LM Studio；配置只要 base_url + api_key + model
- [ ] 保留 `claude-cli` 为默认（本机零配置、零额外成本）；原 `api` 重命名为 `anthropic`
- [ ] `config.yaml` 从单个字符串改成 provider 块：
  ```yaml
  llm:
    provider: claude-cli      # claude-cli | anthropic | openai-compat
    base_url:                 # openai-compat 必填
    api_key:                  # 也可从环境变量 DDLHUB_API_KEY 读
    model:
    timeout_seconds: 240
    batch_size: 60
  ```
- [ ] 配置向导 `python -m ddlhub.setup_llm`：交互选服务商 → 填 key/base_url → 发一条探测请求验证连通与 JSON 服从性 → 写回 config.yaml
- [ ] 开发者推荐免费 LLM，写进向导首屏和 README：
  - 首选 `claude-cli`（项目默认，无需任何 key）
  - 硅基流动 SiliconFlow：注册送额度，Qwen / GLM 小模型有免费档
  - 智谱 GLM-4-Flash：长期免费
  - Google Gemini Flash：有免费额度
  - DeepSeek：几乎免费
  - 本地 Ollama / LM Studio：完全离线，但要机器扛得住
  - 说明：免费模型「只输出 JSON」的服从性差，靠 `_extract_json_array` 的宽松解析兜底
- [ ] 启动自检：provider 不可用时给明确报错，而不是每天定时任务静默失败
- [ ] 横向评测：拿 A/B 组测试用例当基准（见下方「测试用例」），同一批消息跑各家，比对抽取准确率与耗时，结果写进 README
- [ ] 成本与限流：把每次调用的 token 数与耗时记进 `runs` 表，方便挑模型

风险点：不同模型对结构化输出的服从度差异很大，弱模型可能要给 per-provider 追加 few-shot 示例；`_clean_due` 的年份窗口、`idx` 校验这些后处理要保持 provider 无关。

## 6. Telegram 手动录入（代码完成，待实测）

`ddlhub/intake.py`，长轮询 `getUpdates`（不需要公网地址），offset 存 `data/tg_offset.json`。

- [x] `/add_homework` 开一轮会话 → 文字/截图/文件逐条收集 → `/done` 统一解析
- [x] `/cancel` 丢弃，`/help` 说明；命令菜单用 `setMyCommands` 注册
- [x] 回执分两段：**原始解析**（标题/课程/类型/截止/置信/依据）+ **合并结果**（新增/改期/已有）
- [x] 聊天白名单 `intake.allowed_chat_ids`，留空回退到 `notify.telegram.chat_id`
- [x] 会话超时 `session_timeout_minutes`，超时的内容直接丢弃并告知
- [x] `pipeline.extract_rows()` 只抽刚录入的那几条，不把群采集攒的几十条一起拖进来
- [x] `db.upsert_task_detail()` 返回合并细节；`db._same_day()` 让同一天内钟点浮动不算改期
- [x] **直接复用提醒 bot**（`peter_alex_homework_alert_bot`），不用新建。它只发消息、
      从不拉更新，`getUpdates` 槽位是空的（2026-10-30 实测 `pending_update_count: 1`，
      说明确实没人取过）。没配 `intake.token` 时自动回退到 `notify.telegram.token`。
- [x] 首次启动跳过历史积压（`getUpdates offset=-1` 取最后一条），不会回放陈年消息
- [x] **服务已重启并上线**（2026-10-30 13:5x，日志 `[intake] 开始长轮询`），等真实消息验证
- [ ] 真实收发一次：向 @peter_alex_homework_alert_bot 发 `/add_homework` → 发内容 → `/done`
- [ ] 图片走 `claude -p` 远程解析，一张要十几秒；大图要不要本地 OCR 待定

## 测试用例（已设计，尚未落文件）

- [ ] A 组 25 条确定性单测（mock 模型输出）：`_extract_json_array` / `_clean_due` / `_clamp` / `_fmt_msg` / `extract_batch` 过滤
- [ ] B 组语义金标准（真调模型）：时间解析 9 条、噪音过滤 7 条、内容正确性 6 条、脏数据 6 条、学期末标注 2 条、批处理 2 条
- [ ] C 组落库合并 7 条（走 `db.upsert_task`，含跨课程误合并这个已知风险）
- [ ] B 组用例年份依赖 `_clean_due` 的 [当前年-2, 当前年+3] 窗口，需用相对当前年构造日期，否则两年后失效

## 其它已知问题

- 雨课堂 `sessionid` 几小时到几天过期，过期后每日任务静默失败。可以加一条：连续 N 天没有新消息时提醒用户换 cookie。
- `notify.daily_time` 依赖 `python run.py` 常驻。开机自启（计划任务）待定，用户暂未确认。
