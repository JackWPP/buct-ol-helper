# 雨课堂网课助手（BUCT Edition）

一个可以常年挂在云服务器上的**个人网课进度托管工具**：微信扫码登录并复用会话、看板查看刷课进度、课程刷完 / 登录失效 / 平台风控时通过 **飞书 / 邮件** 通知你。

> 目标平台：雨课堂专业版（默认 `https://grabuct.yuketang.cn`，可在配置中改成任意 `<学校>.yuketang.cn`）。

---

## 功能一览

| 能力 | 说明 |
| --- | --- |
| 微信扫码登录 | 与网页端完全一致的微信开放平台扫码流程，二维码直接显示在看板上 |
| 会话复用 | `sessionid` 持久化到本地，重启服务无需重新扫码；失效自动检测并通知 |
| Cookie 导入 | 备用登录方式：从浏览器复制 Cookie 粘贴即可 |
| **只刷网络课程** | 同步时自动识别「网络课程」并勾选，教师自建课自动排除（可在设置中关闭该规则） |
| 进度看板 | 课程 / 章节 / 视频三级进度、已看时长、剩余时长、预计完成时间、运行日志 |
| **实时动态** | 1.5 秒刷新的实时面板：当前观看位置、进度、心跳批次数、平台响应 |
| **跳转平台** | 每门课 / 每个章节都有「平台 ↗」链接，作业、考试、讨论会自动标注「需手动完成」并给出直达链接 |
| 自动刷课 | 按平台真实播放器行为产生心跳，支持并行观看、暂停 / 继续、失败重试、手动跳过 |
| 平台规则自适应 | 自动识别平台限速与风控信号，自动降速、冷却退避（不会硬刚平台） |
| **定时汇报** | 默认每小时把进度汇总（含正在观看的视频）推送到飞书/邮件 |
| 自动回填进度 | 默认每 10 分钟从平台回填一次真实进度（`last_point`/`watch_length`），不会长期停留在旧数据 |
| 通知提醒 | 登录成功 / 登录过期 / 每小时进度 / 课程刷完 / 全部完成 / 风控降速 / 失败，支持飞书机器人与 SMTP 邮件 |
| 云端托管 | Docker + docker-compose 一键部署，数据卷持久化，看板可加访问口令 |
| **命令行 CLI** | `ykt` 一条命令完成查状态、同步、勾选、启停、汇报，支持 `--json` 供程序/Agent 调用 |
| **MCP 服务** | 内置 MCP（stdio）服务，`ykt setup` 一键配置到 Codex / Claude Code / WorkBuddy，让 AI Agent 直接驱动 |
| **Agent 技能** | 随附 `SKILL.md` 技能包，教会 Agent 何时用、怎么用、有哪些纪律（不擅自提速、不代做作业） |
| **桌面客户端** | Electron 应用：托盘常驻、一键连服务、看板秒开，GitHub Release 提供安装包 |
| 诊断探针 | 一条命令导出关键接口原始返回，平台改版时快速定位 |

---

## 工作原理（为什么相对"优雅"）

从平台前端实现中还原了真实的播放行为，而不是简单粗暴地刷进度：

```
播放视频时：每 5 秒产生一条 et="heartbeat" 事件（cp = 当前播放位置）
           ↓ 累积
每 30 秒把累积事件打包 POST 到 /video-log/heartbeat/（单包 ≤ 50 条，与前端上限一致）
           ↓ 周期性核对
每 2 分钟查询 /video-log/get_video_watch_progress/ 核对平台实际记录的 rate
```

在此之上加入自适应逻辑：

- 心跳响应出现异常提示（`anomaly`）→ 立即**降速一半 + 冷却 60/300/900 秒**，并通知你；
- 响应提示限流（`Expected available in X second`）→ 按提示等待后继续；
- 平台进度长时间不增长 → 判定为限速，自动降速并重新对齐平台记录的位置；
- 全局请求节流 + 随机抖动，避免固定节奏的机器特征。

默认参数：**1.0 倍速 + 2 路并行 + 5 秒心跳**，即"真实播放速度"，风险最低。倍速与并发可在看板上调整。

---

## 快速开始（uv）

项目使用 [uv](https://github.com/astral-sh/uv) 管理环境，并已默认配置**清华 PyPI 镜像**（`pyproject.toml` 中 `[[tool.uv.index]]`），国内网络无需额外设置。

```bash
# 1. 安装 uv（若已安装可跳过）
pip install uv                      # 或：curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. 安装依赖（自动创建 .venv，使用 Python 3.12）
cd buct_OL_HELPER
uv sync

# 3. 生成配置
cp config.example.yaml config.yaml   # 按需修改

# 4. 启动
uv run python run.py
```

打开 <http://127.0.0.1:8765> 即可看到看板。

> 若需要走官方 PyPI：`uv sync --default-index https://pypi.org/simple`
> 若公司/校园网有私有源：`UV_DEFAULT_INDEX=https://your.mirror/simple uv sync`

### 使用流程

1. 右上角 **扫码登录** → 微信扫码（二维码过期会自动刷新）；
2. **同步课程** → 拉取课程与章节列表（会自动只勾选「网络课程」）；
3. 如需调整：在课程卡片上手动勾选/取消 **参与刷课**，或点 **一键勾选网络课** 重新套用规则；
4. 点 **全部开始**，或对单门课点 **开始**；
5. 在 **实时动态** 区查看当前正在观看的视频（位置、进度、心跳批次数、平台响应）；
6. 需要做作业/考试时，点课程或章节右侧的 **平台 ↗**（作业、考试、讨论会标注「需手动完成」）；
7. 之后关闭页面即可，服务会在服务器上继续跑，并每小时汇报一次进度。

顶部按钮说明：**同步课程**（拉列表 + 按规则勾选）、**一键勾选网络课**、**刷新进度**（回填平台真实进度 + 补全未知时长）、**立即汇报**（马上发一次进度到飞书/邮件）、**全部开始/暂停**。

---

## 配置说明（config.yaml）

```yaml
server:
  port: 8765
  access_token: ""        # 公网部署强烈建议设置：进看板需要输入口令
  data_dir: "data"        # SQLite 与会话文件目录

account:
  base_url: "https://grabuct.yuketang.cn"
  university_id: null     # 登录后自动探测，探测失败可手工指定

engine:
  speed: 1.0                        # 播放倍速（1.0 = 真实速度，>2.0 风险明显上升）
  max_parallel_videos: 2            # 并行视频数（课程之间并行，课程内部串行）
  heartbeat_tick: 5.0               # 心跳节拍（秒/条，真实前端为 5）
  flush_interval: 30.0              # 批量上报间隔（秒，真实前端约 30）
  progress_check_interval: 120.0    # 进度核对间隔（秒）
  retry: 3                          # 单个视频失败重试次数
  auto_start: false                 # 服务启动后自动开始
  auto_select_network_courses: true # 同步时自动只勾选「网络课程」
  auto_refresh_minutes: 10          # 自动回填平台真实进度的间隔
  report_interval_minutes: 60       # 每小时进度汇报（≥10）
  active_hours: null                # 例如 "07:00-23:30"，深夜暂停更贴近真人
  cdn_tag: "bd"                     # 心跳中的 CDN 标识
  dry_run: false                    # 演练模式：只走逻辑不发送心跳（上线前自检）

notify:
  enabled: true
  feishu:
    webhook: "https://open.feishu.cn/open-apis/bot/v2/hook/xxxx"
    secret: ""                      # 机器人开启签名校验时填写
  email:
    smtp_host: "smtp.qq.com"
    smtp_port: 465
    use_ssl: true
    username: "you@qq.com"
    password: "邮箱授权码"
    to: ["you@qq.com"]
  events:                           # 各事件通知开关
    login: true
    login_expired: true
    course_completed: true
    all_completed: true
    video_completed: false          # 默认关，避免刷屏
    anomaly: true
    errors: true
```

所有 `engine.*` 与 `notify.*` 参数都可以在**看板「设置」中直接修改**（保存后持久化到数据库，即时生效，无需重启）。

### 飞书机器人（推荐，手机上秒收提醒）

1. 打开飞书，**新建一个群**（可以只拉自己，或用任意已有的群）；
2. 群内 → 右上角 **设置** → **群机器人** → **添加机器人** → 选择 **自定义机器人（Custom Bot）**；
3. 填个名字（如「雨课堂助手」）→ 下一步 → **安全设置**，二选一：
   - **签名校验**（推荐）：飞书会生成一个密钥（形如 `xxxxxxxx`），把它填到工具的「飞书签名密钥」；
   - **自定义关键词**：填 `雨课堂助手` —— 工具发送的所有消息都会带「【雨课堂助手】」前缀，可直接通过校验；
   - 不建议选「IP 白名单」：家里/公司/云服务器的 IP 一旦变化就会收不到消息。
4. 复制 **Webhook 地址**（形如 `https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx-xxxx-xxxx`）；
5. 回到看板 → **设置** → 粘贴 **飞书机器人 Webhook**（启用签名校验的再填密钥）→ **保存设置** → 点 **发送测试通知**，群里马上会收到一条测试消息。

也可以直接写进 `config.yaml`：

```yaml
notify:
  enabled: true
  feishu:
    webhook: "https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx"
    secret: ""            # 开启签名校验时填写
  events:
    login: true
    login_expired: true
    course_completed: true
    all_completed: true
    anomaly: true
    errors: true
```

常见失败原因（会显示在看板提示与运行日志里）：

| 错误 | 原因 |
| --- | --- |
| `19021 sign match fail` | 签名密钥填错或漏填（开启签名校验却没填 `secret`） |
| `19024 Key Words Not Found` | 关键词未匹配：安全设置里填 `雨课堂助手` |
| `19001 param invalid` | Webhook 地址不完整（中间被截断） |
| 一直收不到 | Webhook 填成了别的群的地址 / 机器人被移出群 |

### 邮件

以 QQ 邮箱为例：SMTP 服务器 `smtp.qq.com`，端口 `465`，密码填**授权码**（不是登录密码）。保存后点 **发送测试通知**。

---

## 云服务器部署

### 方式一：Docker Compose（推荐）

```bash
# 服务器上
git clone <你的仓库> && cd buct_OL_HELPER
cp config.example.yaml config.yaml && vim config.yaml   # 设置 access_token / 通知
docker compose up -d --build
docker compose logs -f
```

- `Dockerfile` 使用 `python:3.12-slim` + uv，并默认走清华镜像源；
  如需换源：`docker compose build --build-arg UV_DEFAULT_INDEX=https://pypi.org/simple`。
- 数据持久化：`./data`（SQLite + 会话）与 `./config.yaml` 挂载进容器，重建容器不丢登录态。
- 端口默认 `8765`，建议用 Nginx/Caddy 反代并加 HTTPS。

### 方式二：裸机 + systemd

```bash
uv sync
cat > /etc/systemd/system/ykt-helper.service <<'EOF'
[Unit]
Description=Yuketang Helper
After=network.target

[Service]
WorkingDirectory=/opt/buct_OL_HELPER
ExecStart=/usr/local/bin/uv run python run.py
Restart=always

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload && systemctl enable --now ykt-helper
```

### 安全建议

- **必须设置 `server.access_token`**，否则任何知道地址的人都能操作你的网课；
- `data/session.json` 里的 `sessionid` 等价于登录凭证，注意服务器权限（不要提交到 Git，已在 `.gitignore` 中排除）；
- 建议只开放看板端口给你自己（安全组白名单 / 反代加 Basic Auth）。

---

## 常见问题

**1. 登录后课程同步为空 / 接口报错？**
先点一次 **设置 → 发送测试通知** 确认通知正常，然后访问 `GET /api/system/probe`（需登录）导出关键接口的原始返回，对照日志排查平台是否改版。所有接口路径集中在 `app/yuketang/constants.py`，便于快速修正。

**2. 想先干跑验证，不产生真实学习记录？**
设置中打开 **演练模式（dry_run）**，工具会完整走一遍逻辑但不发送心跳。

**3. 某门课一直失败？**
在课程详情里可以看到每个视频的状态与错误原因，支持 **重试 / 跳过 / 重置**。常见原因是该章节不是视频（作业、考试、讨论会被自动跳过）。

**4. 支持哪些内容？**
目前支持 **视频类章节**；作业、考试、测验、讨论、公告等会被自动识别并跳过；音频课件与直播回放暂未实现。

**5. 会不会被平台发现？**
本项目默认按真实播放器行为以 1 倍速运行，并内置降速退避策略，尽可能降低被风控判定的概率，但**平台规则会变化，无法给出任何保证**。请阅读下方免责声明，自行评估风险。

---

## 目录结构

```
buct_OL_HELPER/
├── pyproject.toml / uv.lock     # uv 环境与依赖（默认清华镜像）
├── config.example.yaml          # 配置模板
├── run.py                       # 启动入口
├── Dockerfile / docker-compose.yml
├── app/
│   ├── main.py                  # FastAPI 应用（看板 + 访问口令）
│   ├── runtime.py               # 运行时容器（会话 / 引擎 / 通知 / 后台任务）
│   ├── models.py  db.py         # SQLite 数据模型
│   ├── jobs.py                  # 课程同步 / 进度回填
│   ├── notify.py                # 飞书 + 邮件通知
│   ├── settings_store.py        # 可在线调整的参数
│   ├── yuketang/                # 平台协议层（登录 / 课程 / 心跳）
│   ├── engine/                  # 刷课引擎（调度 + 观看 Worker）
│   └── web/                     # 看板前端（原生 HTML/CSS/JS）
└── data/                        # 运行时数据（数据库、会话）
```

---

## 命令行与 AI Agent 集成（CLI / MCP / 技能）

除了看板，这套工具还提供 **CLI**、**MCP 服务** 与 **Agent 技能**，可以让 Codex / Claude Code / WorkBuddy 等 AI Agent 直接驱动它干活。

### 1. 一键配置到 AI Agent

```bash
uv run python -m app.cli setup --client all --url http://127.0.0.1:8765 --token <访问口令>
# 也可只装某几个：--client codex claude workbuddy
```

它会做四件事：

1. 用官方命令 `codex mcp add` / `claude mcp add` 注册名为 **ykt** 的 MCP 服务（stdio）；
2. 把技能 **ykt-helper** 软链到 `~/.codex/skills`、`~/.claude/skills`、`~/.workbuddy/skills`（WorkBuddy 兼容 OpenClaw 技能生态）；
3. 安装命令行入口 `~/.local/bin/ykt`；
4. 把服务地址与口令记到 `~/.ykt-helper/config.json`（权限 600）。

验证：`codex mcp list` 应看到 `ykt`，`claude mcp list` 显示 `✔ Connected`，`ykt doctor` 显示服务连通。

### 2. 命令行用法（人和 Agent 通用）

| 命令 | 说明 |
| --- | --- |
| `ykt status` | 登录 / 引擎 / 进度概览 |
| `ykt live` | 当前正在观看的视频（位置、进度、心跳批次） |
| `ykt courses [--network-only]` | 课程列表 |
| `ykt leaves <课程id>` | 章节明细（含"需手动完成"的作业/考试） |
| `ykt sync` / `enable-network` | 同步课程 / 只勾选网络课程 |
| `ykt start` / `stop` | 全部开始 / 暂停 |
| `ykt course <id> start\|pause\|enable\|disable\|refresh` | 单门课操作 |
| `ykt leaf <id> retry\|skip\|reset` | 单个章节操作 |
| `ykt report` | 立即发送进度汇报（飞书/邮件） |
| `ykt logs -n 50` | 最近日志 |
| `ykt settings --set engine.speed=1.5` | 查看 / 修改参数 |
| `ykt login [--cookie "..."]` | 生成扫码二维码图片 / 用 Cookie 登录 |
| `ykt doctor` / `probe` / `auth-check` | 自检 / 接口诊断 / 校验登录态 |
| `ykt serve` | 在本机启动看板服务 |

所有命令都支持 `--json`（放在子命令前或后都可以），方便程序与 Agent 解析。服务地址可用 `--url/--token`、环境变量 `YKT_URL/YKT_TOKEN` 或 `~/.ykt-helper/config.json` 指定。

### 3. MCP 工具

`ykt_` 前缀，共 19 个：`ykt_status`、`ykt_live`、`ykt_list_courses`、`ykt_list_leaves`、`ykt_get_logs`、`ykt_sync_courses`、`ykt_enable_network_courses`、`ykt_start`、`ykt_stop`、`ykt_course_action`、`ykt_leaf_action`、`ykt_progress_report`、`ykt_refresh_progress`、`ykt_get_settings`、`ykt_update_settings`、`ykt_login_status`、`ykt_login_qr`、`ykt_login_with_cookie`，以及兜底的 `ykt_cli`。

与 Agent 对话示例：

> **你**：帮我看下网课刷到哪了，还剩多久？
> **Agent**：*调用 `ykt_status`* → 已完成 4/390 个视频，已看 1.2 小时，预计还需 23.5 小时，当前正在看《科研伦理》视频 2。
>
> **你**：把剩下的网络课都挂上。
> **Agent**：*`ykt_enable_network_courses` → `ykt_start`* → 已勾选 7 门网络课程并开始，2 路并行。

技能里写明了纪律：**不擅自调高倍速/并发**、**作业与考试不代做**（只把平台链接给用户）。

## 桌面客户端（Electron）

Release 页面提供 macOS 安装包：`ykt-helper-desktop-1.0.0-arm64.dmg` / `-mac.zip`（Apple Silicon）。

- **托盘常驻**：菜单栏小图标，随时查看进度、开始/暂停刷课、立即汇报、打开看板；
- **一键连服务**：首次打开填「服务地址 + 访问口令」→ 测试连接 → 保存即进入看板；
- **本机托管**（可选）：选择项目目录后由桌面端拉起本机服务（需本机装有 `uv`）。

> macOS 未签名应用首次打开若提示"无法验证开发者"：右键 → 打开，或执行
> `xattr -dr com.apple.quarantine /Applications/雨课堂网课助手.app`

自行构建：

```bash
cd desktop
npm install          # 国内网络建议：ELECTRON_MIRROR=https://registry.npmmirror.com/-/binary/electron/
npm run dist:mac     # 产物在 desktop/release/
```

## 平台接口要点（维护者参考）

平台改版时，优先参考本节 + `app/yuketang/constants.py`，并用 `GET /api/system/probe` 导出原始返回核对。
以下为 **grabuct.yuketang.cn（BUCT，changjiang 集群）实测**结果：

| 环节 | 接口 / 参数 | 关键点 |
| --- | --- | --- |
| 扫码登录 | `POST /api/v3/user/login/wechat-auth-param` → `open.weixin.qq.com/connect/qrconnect` → 轮询 `lp.open.weixin.qq.com/connect/l/qrconnect` → 回调 `changjiang.yuketang.cn/api/v3/user/login/wechat-web-callback` | 回调后取 `sessionid`；首次访问 `/v/course_meta/user_info` 取 `csrftoken` |
| 课程列表 | `GET /v2/api/web/courses/list?identity=2` | 需 `xtbz: cloud` |
| 课程详情 | `GET /v2/api/web/classrooms/{classroom_id}?role=5` | 返回 `free_sku_id`、**`uv_id`（学校标识，用于自动探测）**、`course_sign` |
| 章节列表 | `GET /c27/online_courseware/schedule/score_detail/single/{sku_id}/0/` | `leaf_type`: **0=视频**、3=推荐、4=讨论、5=考试、6=作业；教师自建课可能返回空 |
| 叶子详情 | `GET /mooc-api/v1/lms/learn/leaf_info/{classroom_id}/{leaf_id}/` | **必须带 `xtbz: ykt` + `classroom-id` 头**，否则报 `80005 you did not buy this product` / `40000 CLASSROOM ID IS REQUIRED`。返回 `content_info.media.{ccid,duration}`、`user_id`(平台用户 id，与 ykt user_id 不同)、`course_id` |
| 进度查询 | `GET /video-log/get_video_watch_progress/?cid={course_id}&user_id=&classroom_id=&video_type=video&vtype=rate&video_id=&snapshot=1&term=latest&uv_id={uv_id}` | 返回 `rate`、`last_point`(续看位置)、`watch_length`、`video_length`(权威时长)、`completed` |
| 心跳上报 | `POST /video-log/heartbeat/` body `{"heart_data":[ ... ]}` | `et` 有白名单，非法值返回 `Partial data anomaly` |
| 心跳字段 | `i,et,p,n,lob,cp,fp,tp,sp,ts,u,uip,c,v,skuid,classroomid,cc,d,pg,sq,t,cards_id,slide,v_url` | `c`=course_id、`v`=leaf_id、`cc`=media ccid、`d`=时长、`u`=平台 user_id |
| 心跳节奏 | 播放中每 5 秒一条 `et="heartbeat"`；每 30 秒批量 POST 一次（≤50 条） | 与前端 `sendHeartbeat` 节拍一致 |

**`et` 事件名白名单（实测）**：

- 合法：`loadstart`、`loadeddata`、`play`、`playing`、`pause`、`waiting`、`stalled`、`error`、`ratechange`、`seeking`、`heartbeat`、`videoend`
- 非法：`ended`、`canplay`、`canplaythrough`、`timeupdate`、`seeked`、`loadedmetadata` 等（会触发 anomaly，已从代码中移除）

已适配的能力：自动探测 `uv_id`、`last_point` 续看、`video_length` 补全时长、`anomaly` 降速冷却、连续异常熔断放弃章节。

## 免责声明

本项目仅供**个人学习进度管理**与**技术研究**使用。自动化访问可能违反平台用户协议与所在学校的学术规范，使用带来的账号风控、进度清零、成绩作废等后果由使用者自行承担。请仅在你的**本人账号**上使用，并优先遵守学校与平台的规定；如课程进度出现异常，应联系任课教师或平台客服解决。作者不对任何使用后果负责。
