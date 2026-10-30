# 作业 DDL 汇总

把微信群 / QQ 群 / 雨课堂里的作业截止时间抽出来，汇总成一个网页看板，并支持 Telegram 提醒和日历订阅。

```
数据源 ──→ messages（原始消息库）
              │  LLM 抽取（复用本机 claude 命令，无需额外 API key）
              ▼
           tasks（作业任务，支持自动识别"延期"并更新）
              │
     ┌────────┼────────┐
     ▼        ▼        ▼
  网页看板  /calendar.ics  Telegram 推送
```

## 快速开始

```bash
cd ddl-hub
pip install -r requirements.txt

cp config.example.yaml config.yaml   # 只在第一次：复制配置模板后再填自己的 Cookie/token
python run.py                       # 起服务 → http://127.0.0.1:8848
python run.py --once                # 立即抽取一次，终端打印摘要
python run.py --collect --once      # 先采集已启用的数据源，再抽取
python run.py --once --push         # 抽取 + 推送到 Telegram
```

`config.yaml` 和 `data/` 已在 `.gitignore` 里，不会进版本库 —— 前者含雨课堂 Cookie 和机器人 token，后者是你的作业数据。

网页上可以直接点「粘贴消息」，把群聊记录贴进去，会立刻分析入库。

## 手机上看

默认只监听 `127.0.0.1`，手机访问不了。两种办法：

- 改 `config.yaml` 里 `server.host: 0.0.0.0`，然后用电脑的局域网 IP 访问（同一 WiFi 下）；
- 或者用 Tailscale / ngrok 之类做内网穿透，安全些。

日历订阅同理：把 `http://<可访问地址>:8848/calendar.ics` 填进手机日历的「添加订阅日历」。

## 提醒

- **Telegram**：`config.yaml` 里 `notify.telegram.enabled: true`。不填 token 时会自动读取本机 Telegram 插件已配置的 token（`~/.claude/channels/telegram/.env`）。
- **每日定时**：`notify.daily_time: "08:00"`，服务在跑就会每天自动抽取 + 推送。
- **日历**：`.ics` 里内置了两个 VALARM（提前 1 天、提前 3 小时），订阅后系统日历会自己响。

## 手动录入（Telegram）

默认就复用提醒用的那个 bot，不用另外建。`config.yaml`：

```yaml
intake:
  enabled: true
  token: ""                     # 留空=复用 notify.telegram.token
  session_timeout_minutes: 30
  allowed_chat_ids: []          # 留空=只接受 notify.telegram.chat_id
```

提醒 bot 平时只发 `sendMessage`、从不拉更新，所以它的 `getUpdates` 槽位是空的，
拿来收消息不冲突。**但**如果你把这个 bot 同时也接给了别的程序做长轮询
（比如 Claude Code 的 Telegram 插件），两边就会抢同一条更新——那种情况下才需要
在 `intake.token` 里填另一个 bot。

然后在 Telegram 里：

1. `/add_homework` 开一轮录入
2. 直接发作业文字、截图或 .doc/.pdf/.pptx 文件，发一条存一条（不会立刻解析）
3. `/done` 统一解析 → 并入看板 → 回报**原始解析结果**和**合并结果**（新增 / 改期 / 已存在）
4. `/cancel` 丢弃本轮

同一个作业重复录入不会产生两条：标题指纹 + 章号 + 课名 + 截止日会一起比对；
同一天内只差钟点（`00:00` vs `23:59`）视为同一条，不报「改期」。
Telegram 的命令名只允许字母数字下划线，所以 BotFather 菜单里注册的是 `/add_homework`；
`/add-homework` 这种写法程序也认，只是不会出现在命令联想里。

## 数据源

| 来源 | 状态 | 接入方式 |
|---|---|---|
| 手动粘贴 | ✅ 可用 | 网页「粘贴消息」 |
| 手动录入 | ✅ 可用 | Telegram bot `/add_homework`，支持文字/截图/文件 |
| 微信群 | ✅ 待启用 | wxauto（UI 自动化），需微信 3.9.x |
| 雨课堂 | ✅ 可用 | Cookie + `logs/learn` 接口 |
| QQ 群 | ✅ 待启用 | NapCat（OneBot v11 HTTP），扫码登录小号 |
| 群文件 | ✅ 可用 | 自动解析群里的 .doc/.docx/.pdf/.pptx/.xlsx |

启用的数据源会在每天 `daily_time` 自动采集；手动跑一次用 `python run.py --collect --once`。

### 微信群（wxauto）

1. 微信降到 **3.9.x**（你计划用 3.9.12）并保持登录；
2. `config.yaml` 里 `sources.wechat.enabled: true`，`groups` 填群名（和会话列表完全一致）；
3. `python run.py --collect --once` 验证。

只读界面、不碰协议。代码优先用 `GetNextNewMessage()`（只抓有新消息的会话，会顺手把红点消掉），没有这个接口时退回逐群 `GetAllMessage()` + 去重。`groups` 留空时只处理新消息接口返回的会话。

### 雨课堂

1. 浏览器登录雨课堂，F12 → Network，复制请求头里的整条 Cookie（必须含 `sessionid`）；
2. 粘进 `config.yaml` 的 `sources.yuketang.cookie`；
3. 学校有独立入口就改 `host`（如 `xxx.yuketang.cn`），否则保持 `www.yuketang.cn`；
4. `python run.py --collect --once` 验证。

走 `GET /v2/api/web/logs/learn/{classroom_id}?actype=-1&page=0&offset=100&sort=-1` 拉课程活动：`type` 19 = 作业、20 = 小测，截止时间取 `content.score_d`（毫秒时间戳），`data.has_more` 控制翻页。两条过滤规则：截止时间早于 7 天前的不入库；截止时间比创建时间晚 60 天以上的视为「教师没设截止、系统落到学期末」的默认值（常见 2027-01-31），丢弃。

`sessionid` 有效期只有几小时到几天，过期后采集会报认证失败，重新复制 Cookie 即可。解析异常时原始返回会写到 `data/yt_debug.json` 供排查。

### QQ 群（NapCat）

1. 装 [NapCat](https://github.com/NapNeko/NapCatQQ)，用小号扫码登录，开启 HTTP 服务（默认 `127.0.0.1:3000`）；
2. `config.yaml` 里 `sources.qq.enabled: true`，`onebot_http` 填地址，有 token 就填 `token`；
3. `groups` 填群名或群号，留空表示所有群；`history_count` 控制每次回看多少条（默认 100）。

拉的是 `get_group_list` + `get_group_msg_history`。群文件消息缺直链时会调 `get_group_file_url` 换一个再下载。

### 群文件解析

老师常把作业塞进 `作业.doc` 这类附件。采集到文件消息后会自动下载并抽成纯文本，再交给 LLM：

| 格式 | 解析方式 |
|---|---|
| `.docx` | zip + XML（纯标准库，最快） |
| `.doc` | Word COM（本机已装 Office） |
| `.pdf` | PyMuPDF |
| `.pptx` | python-pptx |
| `.xlsx` | openpyxl |
| `.txt/.md/.csv/.json` | 直接读 |

全部用本机已有的组件，不需要额外安装。数据脏的地方做了三层兜底：

1. **本地解析结果不可用**（空文本、二进制乱码、扫描件）→ 自动改走 `claude` 读文件，比长度后取更好的那份；
2. **本地不认识的格式** → 直接走 `claude` 远端解析；
3. **都不行** → 跳过并在日志里说明，消息仍会以「群文件 XXX（未解析）」入库，至少让 LLM 知道群里发过这个文件。

单文件上限 8 MB，文本截断到 8000 字。微信侧的文件从 `Documents/WeChat Files` 和 `Documents/xwechat_files` 里按文件名找最近的一份；QQ 侧直接下载到 `data/files/`。

## 目录

```
run.py                  入口：起服务 / 采集 / 抽取
config.yaml             配置
ddlhub/
  db.py                 SQLite 存储、任务指纹与改期合并
  llm.py                LLM 抽取（claude-cli 或 Anthropic API）
  pipeline.py           消息 → 任务的流水线
  ics.py                日历生成
  notify.py             Telegram 摘要推送
  attach.py             群文件 → 纯文本（含 LLM 兜底）
  sources/qq.py         QQ 群采集（NapCat / OneBot）
  sources/wechat.py     微信群采集（wxauto）
  sources/yuketang.py   雨课堂作业/小测采集（Cookie）
  web/app.py            FastAPI 接口
  web/index.html        看板前端（单文件，无构建）
data/ddl.db             数据库
```

## 说明

- **去重与改期**：任务指纹按「课程+标题主体」生成。标题里的括号注释会被忽略，所以「实验报告3」和「实验报告3（延期至11月10日）」会合并成一条，并把旧截止时间记进 `history`。
- **抽取从严**：只保留有明确时限的事项；完全判不出日期的会留着 `due_at = null`，在看板「时间待确认」分组里，置信度低于 0.6 会打黄点提示人工核对。
- **清空演示数据**：删掉 `data/ddl.db` 即可。
