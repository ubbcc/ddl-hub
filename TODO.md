# TODO

当前状态：雨课堂采集已跑通并入库（20 条任务）；QQ / 微信采集器代码已写但**从未用真实数据验证过**，均 `enabled: false`。

## 1. QQ 群消息 + 群文件解析

- [ ] 装 NapCat，用小号扫码登录，确认 HTTP 服务在 `127.0.0.1:3000`
- [ ] `config.yaml` 里 `sources.qq.enabled: true`，`groups` 填一个真实群名
- [ ] 跑 `python run.py --collect --once`，确认文字消息进库
- [ ] 验证群文件链路：`get_group_msg_history` 里的 file 段是否带 `file_id`，`get_group_file_url` 能否换到直链
- [ ] 确认 `attach.py` 解析出的文本确实进了 messages 表、并且被 LLM 抽出任务
- [ ] 风控观察：小号挂一周看是否被限

风险点：NapCat 的 file 段字段名各版本不一（`file` / `name` / `file_id`），可能要按实际返回调整 `_file_segments()`。

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

## 测试用例（已设计，尚未落文件）

- [ ] A 组 25 条确定性单测（mock 模型输出）：`_extract_json_array` / `_clean_due` / `_clamp` / `_fmt_msg` / `extract_batch` 过滤
- [ ] B 组语义金标准（真调模型）：时间解析 9 条、噪音过滤 7 条、内容正确性 6 条、脏数据 6 条、学期末标注 2 条、批处理 2 条
- [ ] C 组落库合并 7 条（走 `db.upsert_task`，含跨课程误合并这个已知风险）
- [ ] B 组用例年份依赖 `_clean_due` 的 [当前年-2, 当前年+3] 窗口，需用相对当前年构造日期，否则两年后失效

## 其它已知问题

- 雨课堂 `sessionid` 几小时到几天过期，过期后每日任务静默失败。可以加一条：连续 N 天没有新消息时提醒用户换 cookie。
- `notify.daily_time` 依赖 `python run.py` 常驻。开机自启（计划任务）待定，用户暂未确认。
