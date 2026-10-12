"""ykt 命令行工具：给人和 AI Agent 用同一套入口。

用法示例：
    ykt status                     查看登录 / 引擎 / 进度概览
    ykt live                       当前正在观看的视频
    ykt courses                    课程列表
    ykt sync                       同步课程（自动只勾选网络课程）
    ykt start / ykt stop           全局开始 / 暂停
    ykt report                     立即发送进度汇报（飞书/邮件）
    ykt settings --set engine.speed=2.0
    ykt setup --client all --url http://127.0.0.1:8765 --token xxx
    ykt serve                      在本机启动看板服务
加 --json 输出结构化结果（便于 Agent 解析）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .client_api import ServiceClient, ServiceError, human_summary, load_config, resolve, save_config
from .integrations import default_clients, install_mcp, install_skill, mcp_command

BIN_DIR = Path(__file__).resolve().parent.parent / "bin"


# ----------------------------------------------------------------------
# 输出
# ----------------------------------------------------------------------


def emit(args, data, text: str) -> None:
    if getattr(args, "json", False):
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(text)


def fmt_clock(seconds: float | None) -> str:
    if not seconds:
        return "--:--"
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


def fmt_dur(seconds: float | None) -> str:
    s = int(seconds or 0)
    if s < 60:
        return f"{s} 秒"
    if s < 3600:
        return f"{s // 60} 分钟"
    return f"{s / 3600:.1f} 小时"


# ----------------------------------------------------------------------
# 命令实现
# ----------------------------------------------------------------------


def cmd_config(args) -> int:
    saved_to = ""
    if args.url or args.token:
        saved_to = str(save_config(url=args.url, token=args.token))
        print(f"已保存到 {saved_to}")
    url, token = resolve()
    config_file = str(Path.home() / ".ykt-helper" / "config.json")
    if args.json:
        print(json.dumps({"url": url, "has_token": bool(token), "config_file": config_file}, ensure_ascii=False, indent=2))
    else:
        print(f"服务地址：{url}")
        print(f"访问口令：{'已设置（***）' if token else '未设置'}")
        print(f"配置文件：{config_file}")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    print(f"启动服务 http://{args.host}:{args.port}（Ctrl+C 退出）")
    uvicorn.run("app.main:app", host=args.host, port=args.port, log_level="info")
    return 0


def cmd_status(args, client: ServiceClient) -> int:
    data = client.overview()
    emit(args, data, human_summary(data))
    return 0


def cmd_live(args, client: ServiceClient) -> int:
    data = client.live()
    active = data.get("active") or []
    if not active:
        emit(args, data, "当前没有正在观看的视频" + ("" if data.get("engine", {}).get("running") else "（引擎已暂停）"))
        return 0
    lines = []
    for a in active:
        lines.append(
            f"▶ {a.get('course', '')} · {a.get('title', '')}\n"
            f"   {fmt_clock(a.get('position'))} / {fmt_clock(a.get('duration'))}"
            f"（{a.get('progress', 0) * 100:.1f}%）  {a.get('speed')}x"
            f"  已上报 {a.get('flushes', 0)} 批 / {a.get('events_sent', 0)} 条心跳"
            f"  {a.get('idle_sec', 0)}s 前更新"
        )
    emit(args, data, "\n".join(lines))
    return 0


def cmd_logs(args, client: ServiceClient) -> int:
    data = client.logs(limit=args.limit)
    logs = list(reversed(data.get("logs") or []))
    text = "\n".join(f"[{l['ts']}] {l['level']:<7} {l['message']}" for l in logs) or "（暂无日志）"
    emit(args, data, text)
    return 0


def cmd_courses(args, client: ServiceClient) -> int:
    data = client.courses()
    rows = data.get("courses") or []
    if args.network_only:
        rows = [c for c in rows if c.get("is_network")]
    lines = []
    for c in rows:
        lines.append(
            f"#{c['id']:<3} {c['name'][:26]:<28} "
            f"{'网络课' if c['is_network'] else '  其他 '} "
            f"{'已勾选' if c['enabled'] else '未勾选'} "
            f"视频 {c['done_videos']}/{c['total_videos']:<4} "
            f"({c['progress'] * 100:.0f}%) 预计还需 {fmt_dur(c.get('eta_seconds'))}"
        )
    emit(args, data, "\n".join(lines) or "（暂无课程，先执行 ykt sync）")
    return 0


def cmd_leaves(args, client: ServiceClient) -> int:
    data = client.leaves(args.course_id)
    lines = []
    for l in data.get("leaves") or []:
        tag = "视频" if l["kind"] == "video" else (l.get("kind_hint") or "其他")
        flag = ""
        if l["status"] == "done":
            flag = "✔"
        elif l["needs_manual"]:
            flag = "需手动完成"
        elif l["status"] == "failed":
            flag = "失败"
        lines.append(
            f"[{l['status']:<8}] {tag:<6} {l['title'][:30]:<32} "
            f"{l['progress'] * 100:>5.1f}%  {fmt_clock(l.get('watched_seconds'))}/{fmt_clock(l.get('duration'))} {flag}"
        )
    emit(args, data, "\n".join(lines))
    return 0


def cmd_settings(args, client: ServiceClient) -> int:
    if args.set:
        values = {}
        for item in args.set:
            if "=" not in item:
                print(f"参数格式应为 KEY=VALUE：{item}", file=sys.stderr)
                return 2
            k, v = item.split("=", 1)
            values[k.strip()] = _coerce(v.strip())
        data = client.update_settings(values)
        emit(args, data, "已更新：" + "，".join(f"{k}={v}" for k, v in values.items()))
        return 0
    data = client.settings()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        for k in sorted(data):
            print(f"{k} = {data[k]}")
    return 0


def _coerce(v: str):
    low = v.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        return v


def cmd_login(args, client: ServiceClient) -> int:
    if args.cookie:
        data = client.import_cookie(args.cookie)
        emit(args, data, f"Cookie 导入成功：{(data.get('user') or {}).get('display_name', '')}")
        return 0
    status = client.auth_status()
    if status.get("logged_in"):
        user = status.get("user") or {}
        emit(args, status, f"已登录：{user.get('display_name') or user.get('user_id')}（uv_id={user.get('uv_id')}）")
        return 0
    data = client.create_qr()
    img = data.get("qr_image", "")
    out = Path.home() / ".ykt-helper" / "login-qr.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    saved = ""
    try:
        import base64
        import re

        m = re.match(r"data:image/(\w+);base64,(.*)", img)
        if m:
            ext = "png" if m.group(1) == "png" else "jpg"
            out = out.with_suffix(f".{ext}")
            out.write_bytes(base64.b64decode(m.group(2)))
            saved = str(out)
    except Exception:
        pass
    key = data.get("key")
    text = [
        "需要使用微信扫码登录。",
        f"二维码图片已保存：{saved or '（保存失败，请改用看板登录）'}",
        f"也可以直接打开看板扫码：{client.url}",
        "正在等待扫码确认…（最多 5 分钟）",
    ]
    print("\n".join(text) if not args.json else json.dumps({"key": key, "qr_path": saved}, ensure_ascii=False))
    if args.no_wait:
        return 0
    deadline = time.time() + 300
    while time.time() < deadline:
        time.sleep(2.5)
        res = client.poll_qr(key)
        st = res.get("status")
        if st in ("waiting", "scanned"):
            if st == "scanned":
                print("已扫码，请在手机上确认…")
            continue
        if st == "confirmed":
            print("登录成功 ✅")
            return 0
        if st in ("expired", "error"):
            print(f"登录未完成：{res.get('message')}")
            return 1
    print("等待超时，请重新执行 ykt login")
    return 1


def cmd_setup(args) -> int:
    """一键配置：记住服务地址 → 注册 MCP 服务 → 安装技能 → 链接 CLI 命令。"""
    url, token = resolve(args.url, args.token)
    if not args.skip_config:
        save_config(url=url, token=token)
        print(f"已记录服务地址：{url}")

    clients = args.client
    if clients == ["all"] or not clients:
        clients = default_clients() or ["codex", "claude", "workbuddy"]
    print(f"目标客户端：{', '.join(clients)}")

    if not args.skill_only:
        mcp_logs = install_mcp(clients, url, token)
        for client, logs in mcp_logs.items():
            for line in logs:
                print(f"  [{client}] {line}")
    if not args.mcp_only:
        skill_logs = install_skill(clients)
        for client, logs in skill_logs.items():
            for line in logs:
                print(f"  [{client}] {line}")

    _link_cli()
    print("\n配置完成。验证建议：")
    print("  codex mcp list        # 看到 ykt 即注册成功")
    print("  ykt status            # 确认能连通服务")
    print(f"  MCP 启动命令：{' '.join(mcp_command())}")
    return 0


def _link_cli() -> None:
    """安装 ~/.local/bin/ykt 命令行入口。

    - 源码运行：软链到仓库内的 bin/ykt（经 uv 运行）
    - 冻结运行（桌面端内置二进制）：生成指向自身的包装脚本，无需 Python / uv
    """
    frozen = bool(getattr(sys, "frozen", False))
    src = Path(sys.executable) if frozen else (BIN_DIR / "ykt")
    if not frozen and not src.exists():
        return
    target = Path.home() / ".local" / "bin" / "ykt"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or target.exists():
            target.unlink()
        if frozen:
            target.write_text(
                f'#!/bin/sh\n# 由「雨课堂网课助手」生成；卸载直接删除本文件即可\nexec "{src}" "$@"\n',
                encoding="utf-8",
            )
            target.chmod(0o755)
        else:
            src.chmod(0o755)
            target.symlink_to(src)
        print(f"已安装命令行入口：{target}")
        path_env = os.environ.get("PATH", "")
        if str(target.parent) not in path_env.split(":"):
            print(f"注意：{target.parent} 不在 PATH 中，可把它加入 PATH 后直接用 `ykt` 命令")
    except Exception as e:  # noqa: BLE001
        hint = "ykt-server <命令>" if frozen else f"uv run --directory {src.parent.parent} python -m app.cli"
        print(f"命令行入口未安装（{e}），可直接用：{hint}")


def cmd_doctor(args) -> int:
    checks = []
    url, token = resolve(args.url, args.token)
    checks.append(("配置文件", str(Path.home() / ".ykt-helper" / "config.json")))
    checks.append(("服务地址", url))
    checks.append(("访问口令", "已设置" if token else "未设置"))
    client = ServiceClient(url, token, timeout=10)
    try:
        overview = client.overview()
        checks.append(("服务连通", "OK"))
        checks.append(("登录状态", "已登录" if overview["account"]["logged_in"] else "未登录"))
        checks.append(("引擎", "运行中" if overview["engine"]["running"] else "已暂停"))
        checks.append(("课程数", str(overview["stats"]["courses"])))
    except ServiceError as e:
        checks.append(("服务连通", f"失败：{e}"))
    for name, value in checks:
        print(f"{name:<8}: {value}")
    return 0


def cmd_mcp(args) -> int:
    """以 stdio 运行 MCP 服务（供 AI Agent 调用）。"""
    from .mcp_server import main as mcp_main

    mcp_main()
    return 0


def cmd_mcp_print(args) -> int:
    url, token = resolve(args.url, args.token)
    cfg = {
        "mcpServers": {
            "ykt": {
                "command": mcp_command()[0],
                "args": mcp_command()[1:],
                "env": {"YKT_URL": url, **({"YKT_TOKEN": token} if token else {})},
            }
        }
    }
    print(json.dumps(cfg, ensure_ascii=False, indent=2))
    return 0


# ----------------------------------------------------------------------
# 参数解析
# ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ykt", description="雨课堂网课助手命令行（人和 Agent 通用）")
    p.add_argument("--url", help="服务地址（默认取 YKT_URL 或 ~/.ykt-helper/config.json）")
    p.add_argument("--token", help="看板访问口令")
    p.add_argument("--json", action="store_true", help="输出 JSON（便于 Agent 解析）")
    sub = p.add_subparsers(dest="command", required=True)

    # 让 --url/--token/--json 写在子命令后面也能生效
    # （default=SUPPRESS 保证子命令未显式给出时不覆盖全局解析结果）
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--url", default=argparse.SUPPRESS)
    common.add_argument("--token", default=argparse.SUPPRESS)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, parents=[common], help=help_text)

    sp = add("config", "查看/设置服务地址与口令")
    sp.set_defaults(func=cmd_config, needs_client=False)

    sp = add("setup", "一键配置到 Codex / Claude / WorkBuddy")
    sp.add_argument("--client", nargs="*", default=["all"], help="codex claude workbuddy all")
    sp.add_argument("--mcp-only", action="store_true", help="只装 MCP，不装技能")
    sp.add_argument("--skill-only", action="store_true", help="只装技能，不装 MCP")
    sp.add_argument("--skip-config", action="store_true", help="不记忆服务地址")
    sp.set_defaults(func=cmd_setup, needs_client=False)

    sp = add("serve", "在本机启动看板服务")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8765)
    sp.set_defaults(func=cmd_serve, needs_client=False)

    add("doctor", "环境与连通性自检").set_defaults(func=cmd_doctor, needs_client=False)
    add("mcp", "以 stdio 运行 MCP 服务（供 AI Agent 调用）").set_defaults(func=cmd_mcp, needs_client=False)
    add("mcp-print", "打印 MCP 配置（手动粘贴用）").set_defaults(func=cmd_mcp_print, needs_client=False)

    add("status", "总览").set_defaults(func=cmd_status, needs_client=True)
    add("live", "当前正在观看的视频").set_defaults(func=cmd_live, needs_client=True)
    add("sync", "同步课程（自动勾选网络课程）").set_defaults(
        func=lambda a, c: emit(a, c.sync(), "已触发同步，稍后用 ykt status 查看结果"), needs_client=True
    )
    add("enable-network", "一键勾选网络课程").set_defaults(func=_enable_network, needs_client=True)
    add("refresh", "刷新平台进度").set_defaults(
        func=lambda a, c: emit(a, c.refresh_all(), "已触发进度刷新"), needs_client=True
    )
    add("start", "全部开始").set_defaults(func=_start, needs_client=True)
    add("stop", "全部暂停").set_defaults(
        func=lambda a, c: emit(a, c.stop(), "已全部暂停（进度已保存）"), needs_client=True
    )
    add("report", "立即发送进度汇报").set_defaults(
        func=lambda a, c: emit(a, c.report(), "进度汇报已发送（飞书/邮件）"), needs_client=True
    )

    sp = add("logs", "最近日志")
    sp.add_argument("-n", "--limit", type=int, default=30)
    sp.set_defaults(func=cmd_logs, needs_client=True)

    sp = add("courses", "课程列表")
    sp.add_argument("--network-only", action="store_true", help="只看网络课程")
    sp.set_defaults(func=cmd_courses, needs_client=True)

    sp = add("leaves", "某门课的章节列表")
    sp.add_argument("course_id", type=int)
    sp.set_defaults(func=cmd_leaves, needs_client=True)

    sp = add("settings", "查看/修改参数")
    sp.add_argument("--set", action="append", metavar="KEY=VALUE")
    sp.set_defaults(func=cmd_settings, needs_client=True)

    sp = add("course", "单门课操作")
    sp.add_argument("course_id", type=int)
    sp.add_argument("action", choices=["start", "pause", "enable", "disable", "refresh"])
    sp.set_defaults(func=_course_action, needs_client=True)

    sp = add("leaf", "章节操作")
    sp.add_argument("leaf_id", type=int)
    sp.add_argument("action", choices=["retry", "skip", "reset"])
    sp.set_defaults(func=_leaf_action, needs_client=True)

    sp = add("login", "登录（扫码或导入 Cookie）")
    sp.add_argument("--cookie", help="直接粘贴浏览器 Cookie")
    sp.add_argument("--no-wait", action="store_true", help="只生成二维码，不等待")
    sp.set_defaults(func=cmd_login, needs_client=True)

    add("probe", "接口诊断").set_defaults(func=_probe, needs_client=True)
    add("auth-check", "校验登录态").set_defaults(func=_auth_check, needs_client=True)
    return p


def _start(args, client: ServiceClient) -> int:
    data = client.start()
    started = data.get("started_courses") or []
    msg = f"已开始 {len(started)} 门课程" if started else "没有勾选任何课程（可执行 ykt enable-network）"
    emit(args, data, msg)
    return 0


def _enable_network(args, client: ServiceClient) -> int:
    data = client.enable_network()
    emit(args, data, f"已勾选 {data.get('enabled', 0)} 门网络课程，排除 {data.get('excluded', 0)} 门")
    return 0


def _course_action(args, client: ServiceClient) -> int:
    action = args.action
    if action in ("enable", "disable"):
        data = client.post(f"/api/courses/{args.course_id}/enable", {"enabled": action == "enable"})
    else:
        data = client.course_action(args.course_id, action)
    emit(args, data, f"课程 #{args.course_id} 已执行 {action}")
    return 0


def _leaf_action(args, client: ServiceClient) -> int:
    data = client.leaf_action(args.leaf_id, args.action)
    emit(args, data, f"章节 #{args.leaf_id} 已执行 {args.action}")
    return 0


def _probe(args, client: ServiceClient) -> int:
    data = client.probe()
    emit(args, data, json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def _auth_check(args, client: ServiceClient) -> int:
    data = client.auth_check()
    emit(args, data, "登录有效" if data.get("ok") else "登录已失效，请重新扫码")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if getattr(args, "needs_client", False):
            client = ServiceClient(args.url, args.token)
            return args.func(args, client)
        return args.func(args)
    except ServiceError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
