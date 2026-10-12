---
name: ykt-helper
description: 雨课堂（yuketang）网课进度助手：用微信扫码登录后托管刷课，提供进度看板、实时观看状态、风控自适应与飞书/邮件通知。触发场景：用户说「网课刷到哪了 / 帮我看下刷课进度 / 把网课挂上 / 同步一下课程 / 只刷网络课 / 暂停刷课 / 给我发个进度汇报 / 有作业要我做吗 / 登录是不是过期了 / 帮我配置 MCP 到 codex 或 workbuddy」。也可用于排查「进度不动 / 心跳异常 / 视频刷失败」。反触发：与雨课堂无关的通用爬虫、其他网课平台（超星、智慧树等）、以及任何代写作业/代考请求，都不要使用本技能。
metadata:
  requires:
    bins: ["uv", "ykt"]
  cliHelp: "ykt --help"
---

# 雨课堂网课助手（ykt-helper）

用命令行 `ykt`（或同名的 MCP 工具）驱动一个常驻的刷课服务：服务负责扫码登录、按平台真实播放器行为产生心跳、把进度同步到看板，并在刷完/异常时通过飞书或邮件通知用户。

## 一、先判断服务是否可达

```bash
ykt doctor              # 环境 + 连通性自检（推荐第一步）
ykt status              # 登录状态、引擎状态、进度概览
```

- `ykt doctor` 报「无法连接服务」→ 该服务没在跑。让用户在本机执行 `ykt serve`（本地模式），或确认远程服务地址：`ykt config --url http://<host>:<port> --token <口令>`。
- 访问口令（access_token）是敏感信息：**不要**把口令打印到聊天里、也不要提交到任何仓库。

## 二、标准工作流

```
① ykt status            看是否已登录、有几门课在跑
② ykt sync              同步课程（会自动只勾选"网络课程"）
③ ykt enable-network    需要时重新套用"只刷网络课"规则
④ ykt start             开始（引擎无已勾选课程时不会启动，需先 ③）
⑤ ykt live              确认真的在刷：位置在走、进度在涨、有平台响应
⑥ ykt report            需要时立刻给用户发一份进度汇报
```

其他常用：

| 需求 | 命令 |
| --- | --- |
| 看某门课的章节明细（含哪些要手动做） | `ykt leaves <course_id>` |
| 单门课开始/暂停/勾选/刷新 | `ykt course <id> start\|pause\|enable\|disable\|refresh` |
| 某个视频失败后重试 / 跳过 / 重置 | `ykt leaf <leaf_id> retry\|skip\|reset` |
| 查看/修改参数 | `ykt settings`、`ykt settings --set engine.speed=1.5` |
| 最近日志 | `ykt logs -n 50` |
| 接口诊断（平台疑似改版） | `ykt probe` |
| 登录二维码（用户要扫码） | `ykt login`（会把二维码存成图片并等待确认） |
| 用户提供 Cookie | `ykt login --cookie "sessionid=...; csrftoken=..."` |

需要结构化输出给程序处理时，任意命令都支持 `--json`。

## 三、必须守住的纪律

1. **倍速与并发不要擅自调高**：默认 `1.0x` + `2` 路并行是按平台真实播放器行为设计的保守值。未经用户明确要求，不要改成 >2x 或 >3 路；平台有风控，"刷得快"会导致视频失败甚至账号异常。
2. **不要替用户做作业/考试**：章节里标了「需手动完成」（作业、考试、讨论、推荐阅读）的内容，助手不处理。此时应把 `platform_url` 给用户，让他自己去平台完成。
3. **登录需要用户本人**：扫码必须用户用微信扫；不要让用户把 sessionid 发到公开渠道。
4. **不要重复登录或反复重试**：`ykt login` 短时间反复调用会被平台/微信限流。
5. **改动前先看状态**：任何 start/stop/参数调整前，先 `ykt status` 说明现状与影响，再执行。
6. **不要修改线上服务的部署**（除非用户明确要求）：只通过 CLI/MCP 操作，不要 ssh 上去改代码或重启容器。

## 四、常见问题处置

| 现象 | 判断与处置 |
| --- | --- |
| `status` 显示未登录 / 日志有「登录已过期」 | 让用户重新扫码：`ykt login`，把生成的二维码图片路径给用户 |
| `live` 里长期没有条目但引擎在跑 | 说明在等待调度或没有勾选课程：`ykt courses` 确认 `已勾选`，必要时 `ykt enable-network` + `ykt start` |
| 进度长时间不动 / 日志有「风控自适应」 | 正常：助手已自动降速退避。可 `ykt logs` 说明；若频繁出现，建议把 `engine.speed` 调回 1.0 |
| 日志有「连续异常已放弃」 | 该章节可能不是视频。`ykt leaves <id>` 看类型，必要时 `ykt leaf <id> skip` |
| 视频显示失败 | `ykt leaf <leaf_id> retry`；多次失败可用 `refresh` 回填平台进度后再判断 |
| 用户想只刷某几门 | `ykt courses` 拿 id → `ykt course <id> enable` / `disable`，然后 `ykt start` |
| 用户问"还剩多久" | `ykt status` 的「剩余 / 预计还需」，并说明是按当前倍速与并行数估算 |

## 五、MCP 与配置

本技能随附 MCP 服务（stdio），工具名以 `ykt_` 开头（`ykt_status`、`ykt_live`、`ykt_list_courses`、`ykt_sync_courses`、`ykt_enable_network_courses`、`ykt_start`、`ykt_stop`、`ykt_course_action`、`ykt_leaf_action`、`ykt_progress_report`、`ykt_get_settings`、`ykt_update_settings`、`ykt_login_status`、`ykt_login_qr`、`ykt_login_with_cookie`、`ykt_cli`）。

如果 MCP 工具不可用，直接用 `ykt` 命令行（能力等价）。

一键把 MCP 与技能装到本机 Agent：

```bash
ykt setup --client all --url http://<host>:<port> --token <口令>
# 或指定：--client codex claude workbuddy
```

验证：`codex mcp list` 应看到 `ykt`；`ykt doctor` 应显示服务连通。
