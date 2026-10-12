"""ykt-helper MCP 服务（stdio 传输）。

给 AI Agent（Codex / Claude Code / WorkBuddy 等）提供直接驱动刷课助手的能力。
注意：stdio 协议占用 stdout，本模块内严禁 print，一律 return。

启动：uv run --directory <项目目录> python -m app.mcp_server
"""
from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

from .client_api import ServiceClient, human_summary

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 兼容 mcp 2.x（MCPServer）与 1.x（FastMCP）
try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # pragma: no cover - mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore

_INSTRUCTIONS = (
    "雨课堂网课助手：查询/托管雨课堂网课进度。"
    "常用流程：ykt_status 看状态 → ykt_sync_courses 同步 → ykt_enable_network_courses 只勾选网络课程 "
    "→ ykt_start 开始 → ykt_live 观察。纪律：不要擅自调高倍速/并发；作业与考试需用户手动完成。"
)
try:
    mcp = _Server(name="ykt-helper", title="雨课堂网课助手", instructions=_INSTRUCTIONS)
except TypeError:  # mcp 1.x 的 FastMCP 只接受 name
    mcp = _Server("ykt-helper")  # type: ignore[call-arg]


def _client() -> ServiceClient:
    return ServiceClient()


def _ok(text: str) -> str:
    return text


def _err(e: Exception) -> str:
    return f"调用失败：{e}"


# ----------------------------------------------------------------------
# 状态查询
# ----------------------------------------------------------------------


@mcp.tool()
def ykt_status() -> str:
    """查看雨课堂网课助手的整体状态：登录是否有效、引擎是否在跑、课程与视频进度、预计剩余时间。"""
    try:
        return human_summary(_client().overview())
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def ykt_live() -> str:
    """查看当前正在观看的视频：课程名、章节名、播放位置、进度百分比、已上报心跳批次与平台响应。"""
    try:
        data = _client().live()
        active = data.get("active") or []
        if not active:
            running = data.get("engine", {}).get("running")
            return "当前没有正在观看的视频" + ("" if running else "（引擎已暂停）")
        lines = []
        for a in active:
            lines.append(
                f"{a.get('course')} · {a.get('title')} | "
                f"{int(a.get('position', 0))}s/{int(a.get('duration', 0))}s "
                f"({(a.get('progress') or 0) * 100:.1f}%) | {a.get('speed')}x | "
                f"上报 {a.get('flushes', 0)} 批 / {a.get('events_sent', 0)} 条 | "
                f"{a.get('idle_sec', 0)}s 前更新"
            )
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def ykt_list_courses(network_only: bool = False) -> str:
    """列出课程（含是否网络课程、是否已勾选、视频完成情况、预计还需时间）。

    Args:
        network_only: 只列出"网络课程"（真正需要刷视频的那类）。
    """
    try:
        rows = _client().courses().get("courses") or []
        if network_only:
            rows = [c for c in rows if c.get("is_network")]
        if not rows:
            return "暂无课程，请先执行 ykt_sync_courses。"
        return "\n".join(
            f"#{c['id']} {c['name']} | {'网络课' if c['is_network'] else '其他'} | "
            f"{'已勾选' if c['enabled'] else '未勾选'} | "
            f"视频 {c['done_videos']}/{c['total_videos']} ({c['progress'] * 100:.0f}%) | "
            f"状态 {c['status']}"
            for c in rows
        )
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def ykt_list_leaves(course_id: int) -> str:
    """列出某门课的章节明细：类型（视频/作业/考试/讨论）、状态、进度、是否需要手动完成。

    Args:
        course_id: 课程 id（先用 ykt_list_courses 获取）。
    """
    try:
        data = _client().leaves(course_id)
        lines = [f"课程：{data['course']['name']}（{data['course']['platform_url']}）"]
        for l in data.get("leaves") or []:
            extra = ""
            if l.get("needs_manual"):
                extra = f" ← 需手动完成（{l.get('kind_hint')}）"
            lines.append(
                f"  [{l['status']}] {l['kind_hint'] or l['kind']} {l['title'][:40]} "
                f"{l['progress'] * 100:.0f}%{extra}"
            )
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def ykt_get_logs(limit: int = 30) -> str:
    """查看助手最近的运行日志（同步、观看、风控、通知等事件）。"""
    try:
        logs = list(reversed(_client().logs(limit=limit).get("logs") or []))
        return "\n".join(f"[{l['ts']}] {l['level']} {l['message']}" for l in logs) or "暂无日志"
    except Exception as e:  # noqa: BLE001
        return _err(e)


# ----------------------------------------------------------------------
# 操作类
# ----------------------------------------------------------------------


@mcp.tool()
def ykt_sync_courses() -> str:
    """同步课程与章节列表（会按规则自动只勾选"网络课程"）。同步是异步任务，稍后用 ykt_status 查看结果。"""
    try:
        _client().sync()
        return "同步任务已启动，约 30-60 秒后可用 ykt_status 查看结果。"
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def ykt_enable_network_courses() -> str:
    """只勾选"网络课程"（有视频的慕课），其余课程不参与自动刷课。"""
    try:
        data = _client().enable_network()
        return f"已勾选 {data.get('enabled', 0)} 门网络课程，排除 {data.get('excluded', 0)} 门其他课程。"
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def ykt_start() -> str:
    """开始自动刷课（对所有已勾选的课程；需先登录）。"""
    try:
        data = _client().start()
        started = data.get("started_courses") or []
        if not started:
            return "没有勾选任何课程：可执行 ykt_enable_network_courses 或 ykt_course_action 勾选。"
        return f"已开始 {len(started)} 门课程：{started}。几秒后可用 ykt_live 看到实际观看情况。"
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def ykt_stop() -> str:
    """暂停自动刷课（进度会保存，可随时再开始）。"""
    try:
        _client().stop()
        return "已全部暂停（进度已保存）。"
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def ykt_course_action(course_id: int, action: str) -> str:
    """对单门课操作。

    Args:
        course_id: 课程 id
        action: start(开始) / pause(暂停) / enable(勾选参与刷课) / disable(取消勾选) / refresh(刷新该课进度)
    """
    try:
        client = _client()
        if action in ("enable", "disable"):
            client.post(f"/api/courses/{course_id}/enable", {"enabled": action == "enable"})
        elif action in ("start", "pause", "refresh"):
            client.course_action(course_id, action)
        else:
            return f"不支持的 action：{action}（可选 start/pause/enable/disable/refresh）"
        return f"课程 #{course_id} 已执行 {action}。"
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def ykt_leaf_action(leaf_id: int, action: str) -> str:
    """对单个章节操作。

    Args:
        leaf_id: 章节 id（来自 ykt_list_leaves）
        action: retry(重试) / skip(跳过) / reset(重置进度后重刷)
    """
    try:
        if action not in ("retry", "skip", "reset"):
            return f"不支持的 action：{action}（可选 retry/skip/reset）"
        _client().leaf_action(leaf_id, action)
        return f"章节 #{leaf_id} 已执行 {action}。"
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def ykt_progress_report() -> str:
    """立即生成并推送一份进度汇报（飞书/邮件），返回汇报正文。"""
    try:
        data = _client().report()
        return data.get("body") or "汇报已发送。"
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def ykt_refresh_progress() -> str:
    """从平台回填真实进度（也会补全未知的视频时长）。耗时约 1-3 分钟。"""
    try:
        _client().refresh_all()
        return "进度刷新已启动（后台执行）。"
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def ykt_get_settings() -> str:
    """查看当前可调参数（倍速、并发、心跳节奏、通知配置等）。"""
    try:
        data = _client().settings()
        return "\n".join(f"{k} = {data[k]}" for k in sorted(data))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def ykt_update_settings(key: str, value: str) -> str:
    """修改一个参数并立即生效（例如倍速、并发数、通知开关）。

    Args:
        key: 参数名，如 engine.speed / engine.max_parallel_videos / notify.enabled
        value: 新值（字符串形式，例如 "2.0" / "3" / "true"）
    """
    low = value.strip().lower()
    if low in ("true", "false"):
        val: object = low == "true"
    else:
        try:
            val = int(value)
        except ValueError:
            try:
                val = float(value)
            except ValueError:
                val = value
    try:
        _client().update_settings({key: val})
        return f"已更新 {key} = {val}"
    except Exception as e:  # noqa: BLE001
        return _err(e)


# ----------------------------------------------------------------------
# 登录
# ----------------------------------------------------------------------


@mcp.tool()
def ykt_login_status() -> str:
    """查看登录状态（是否需要重新扫码）。"""
    try:
        data = _client().auth_status()
        user = data.get("user") or {}
        if data.get("logged_in"):
            return f"已登录：{user.get('display_name') or user.get('user_id')}（状态 {data.get('status')}）"
        return "未登录：需要用户在浏览器/微信完成扫码，或提供 Cookie。"
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def ykt_login_qr() -> str:
    """生成登录二维码（保存成本地图片路径），并给出扫码指引。二维码约 5 分钟有效。"""
    try:
        data = _client().create_qr()
        import base64
        import re

        img = data.get("qr_image", "")
        out = Path.home() / ".ykt-helper" / "login-qr.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        m = re.match(r"data:image/(\w+);base64,(.*)", img)
        if m:
            out = out.with_suffix(".png" if m.group(1) == "png" else ".jpg")
            out.write_bytes(base64.b64decode(m.group(2)))
        return (
            f"二维码已保存：{out}\n请用户用微信扫码并在手机上确认；"
            f"确认后可用 ykt_login_status 检查是否登录成功。\n"
            f"也可以直接打开看板扫码：{_client().url}"
        )
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def ykt_login_with_cookie(cookie: str) -> str:
    """用浏览器 Cookie 登录（用户在浏览器登录雨课堂后，从开发者工具复制完整 Cookie）。

    Args:
        cookie: 形如 "sessionid=xxx; csrftoken=yyy; ..."
    """
    try:
        data = _client().import_cookie(cookie)
        user = data.get("user") or {}
        return f"导入成功，已登录：{user.get('display_name') or user.get('user_id')}"
    except Exception as e:  # noqa: BLE001
        return _err(e)


# ----------------------------------------------------------------------
# 兜底：直接跑 CLI（覆盖上面没暴露的能力，例如 settings 批量修改、doctor）
# ----------------------------------------------------------------------


@mcp.tool()
def ykt_cli(command: str) -> str:
    """执行 ykt 命令行（兜底能力，例如 "status --json"、"logs -n 50"、"settings --set engine.speed=1.5"）。

    Args:
        command: 去掉 `ykt` 前缀的参数串
    """
    try:
        args = shlex.split(command)
    except ValueError as e:
        return f"参数解析失败：{e}"
    blocked = {"setup", "serve", "login"}
    if args and args[0] in blocked:
        return f"出于安全考虑，MCP 不直接执行 `{args[0]}`（请让用户手动运行）。"
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "app.cli", *args],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=180,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        return out + (f"\n[stderr] {err}" if err else "")
    except subprocess.TimeoutExpired:
        return "命令执行超时（>180s）"
    except Exception as e:  # noqa: BLE001
        return _err(e)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
