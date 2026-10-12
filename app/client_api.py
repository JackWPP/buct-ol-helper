"""服务客户端：CLI 与 MCP 共用（通过 HTTP 调用本机/远程的看板服务）。

配置优先级：命令行参数 > 环境变量 > ~/.ykt-helper/config.json > 默认值
环境变量：YKT_URL（或 YKT_BASE_URL）、YKT_TOKEN
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx

DEFAULT_URL = "http://127.0.0.1:8765"
CONFIG_PATH = Path.home() / ".ykt-helper" / "config.json"


class ServiceError(RuntimeError):
    """服务不可用或返回错误。"""


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(**kwargs: Any) -> Path:
    cfg = load_config()
    cfg.update({k: v for k, v in kwargs.items() if v is not None})
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        CONFIG_PATH.chmod(0o600)
    except Exception:
        pass
    return CONFIG_PATH


def resolve(url: str | None = None, token: str | None = None) -> tuple[str, str]:
    cfg = load_config()
    final_url = (
        url
        or os.environ.get("YKT_URL")
        or os.environ.get("YKT_BASE_URL")
        or cfg.get("url")
        or DEFAULT_URL
    )
    final_token = token or os.environ.get("YKT_TOKEN") or cfg.get("token") or ""
    return final_url.rstrip("/"), final_token


class ServiceClient:
    def __init__(self, url: str | None = None, token: str | None = None, timeout: float = 60.0) -> None:
        self.url, self.token = resolve(url, token)
        self.timeout = timeout

    # ---------------- 基础请求 ----------------

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = kwargs.pop("headers", {}) or {}
        if self.token:
            headers["X-Access-Token"] = self.token
        try:
            resp = httpx.request(
                method,
                f"{self.url}{path}",
                headers=headers,
                timeout=self.timeout,
                **kwargs,
            )
        except httpx.ConnectError as e:
            raise ServiceError(
                f"无法连接服务 {self.url}（{e}）。请先启动服务（ykt serve）或用 --url 指定地址。"
            ) from e
        except httpx.TimeoutException as e:
            raise ServiceError(f"服务响应超时（{e}）") from e

        if resp.status_code == 401:
            raise ServiceError("访问口令不正确或未提供：请用 --token 指定，或 ykt config --token <口令>")
        if resp.status_code >= 400:
            detail = ""
            try:
                detail = (resp.json() or {}).get("detail", "")
            except Exception:
                detail = resp.text[:200]
            raise ServiceError(f"服务返回错误 HTTP {resp.status_code}：{detail}")
        try:
            return resp.json()
        except Exception:
            return resp.text

    def get(self, path: str, **params: Any) -> Any:
        return self._request("GET", path, params={k: v for k, v in params.items() if v is not None})

    def post(self, path: str, body: Any = None) -> Any:
        return self._request("POST", path, json=body if body is not None else {})

    # ---------------- 业务封装 ----------------

    def overview(self) -> dict:
        return self.get("/api/system/overview")

    def live(self) -> dict:
        return self.get("/api/system/live")

    def logs(self, limit: int = 30, after_id: int = 0) -> dict:
        return self.get("/api/system/logs", limit=limit, after_id=after_id)

    def courses(self) -> dict:
        return self.get("/api/courses")

    def leaves(self, course_id: int) -> dict:
        return self.get(f"/api/courses/{course_id}/leaves")

    def sync(self) -> dict:
        return self.post("/api/courses/sync")

    def enable_network(self) -> dict:
        return self.post("/api/courses/auto-select")

    def refresh_all(self) -> dict:
        return self.post("/api/courses/refresh-all")

    def course_action(self, course_id: int, action: str) -> dict:
        return self.post(f"/api/courses/{course_id}/{action}")

    def leaf_action(self, leaf_id: int, action: str) -> dict:
        return self.post(f"/api/leaves/{leaf_id}/action", {"action": action})

    def start(self) -> dict:
        return self.post("/api/engine/start")

    def stop(self) -> dict:
        return self.post("/api/engine/stop")

    def engine_status(self) -> dict:
        return self.get("/api/engine/status")

    def report(self) -> dict:
        return self.post("/api/system/report")

    def settings(self) -> dict:
        return self.get("/api/system/settings")

    def update_settings(self, values: dict) -> dict:
        return self.post("/api/system/settings", {"values": values})

    def notify_test(self) -> dict:
        return self.post("/api/system/notify/test")

    def probe(self) -> dict:
        return self.get("/api/system/probe")

    def auth_status(self) -> dict:
        return self.get("/api/auth/status")

    def auth_check(self) -> dict:
        return self.post("/api/auth/check")

    def create_qr(self) -> dict:
        return self.post("/api/auth/qr")

    def poll_qr(self, key: str) -> dict:
        return self.get(f"/api/auth/qr/{key}")

    def import_cookie(self, raw: str) -> dict:
        return self.post("/api/auth/cookie", {"cookie": raw})

    def logout(self) -> dict:
        return self.post("/api/auth/logout")


def human_summary(overview: dict) -> str:
    """把总览转成一段便于人和 agent 阅读的摘要。"""
    acct = overview.get("account", {})
    engine = overview.get("engine", {})
    stats = overview.get("stats", {})
    user = acct.get("user") or {}
    lines = [
        f"服务地址：{acct.get('base_url', '')}",
        f"登录状态：{'已登录' if acct.get('logged_in') else '未登录'}"
        + (f"（{user.get('display_name') or user.get('user_id')}）" if acct.get("logged_in") else ""),
        f"引擎状态：{'运行中' if engine.get('running') else '已暂停'}"
        f"，并行 {engine.get('active_videos', 0)}/{engine.get('max_parallel')} 路，{engine.get('speed')}x",
        f"课程：{stats.get('courses')} 门（启用 {stats.get('enabled_courses')} 门）",
        f"视频：{stats.get('done_videos')}/{stats.get('videos')} 已完成",
        f"已看 {int(stats.get('watched_seconds', 0) // 60)} 分钟，"
        f"剩余约 {int(stats.get('remaining_seconds', 0) // 3600)} 小时，"
        f"预计还需 {stats.get('eta_seconds', 0) / 3600:.1f} 小时",
    ]
    watching = stats.get("watching") or []
    if watching:
        lines.append("正在刷：" + "、".join(watching[:3]))
    return "\n".join(lines)
