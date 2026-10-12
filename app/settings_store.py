"""运行时可调参数：config.yaml 提供默认值，看板上修改的项持久化到数据库。"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from .config import get_config
from .db import db_session
from .models import Setting


def _config_default(key: str) -> Any:
    node: Any = get_config()
    for part in key.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node


class SettingsStore:
    """点分键的配置访问器，例如 engine.speed / notify.feishu.webhook。"""

    def __init__(self) -> None:
        self._cache: dict[str, Any] = {}

    def load(self) -> None:
        with db_session() as s:
            rows = list(s.scalars(select(Setting)))
        self._cache = {}
        for r in rows:
            try:
                self._cache[r.key] = json.loads(r.value)
            except Exception:
                self._cache[r.key] = r.value

    def get(self, key: str, default: Any = None) -> Any:
        if key in self._cache:
            return self._cache[key]
        val = _config_default(key)
        return default if val is None else val

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.get(key, default))
        except (TypeError, ValueError):
            return default

    def get_float(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.get(key, default))
        except (TypeError, ValueError):
            return default

    def get_bool(self, key: str, default: bool = False) -> bool:
        v = self.get(key, default)
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on")
        return bool(v)

    def set(self, key: str, value: Any) -> None:
        with db_session() as s:
            row = s.get(Setting, key)
            payload = json.dumps(value, ensure_ascii=False)
            if row is None:
                s.add(Setting(key=key, value=payload))
            else:
                row.value = payload
        self._cache[key] = value

    def delete(self, key: str) -> None:
        with db_session() as s:
            row = s.get(Setting, key)
            if row is not None:
                s.delete(row)
        self._cache.pop(key, None)

    def snapshot(self) -> dict[str, Any]:
        """所有可调参数的当前值（含 config 默认），用于看板展示。"""
        keys = [
            "engine.speed",
            "engine.max_parallel_videos",
            "engine.heartbeat_tick",
            "engine.flush_interval",
            "engine.progress_check_interval",
            "engine.retry",
            "engine.active_hours",
            "engine.dry_run",
            "engine.auto_start",
            "engine.auto_select_network_courses",
            "engine.auto_refresh_minutes",
            "engine.report_interval_minutes",
            "notify.enabled",
            "notify.feishu.webhook",
            "notify.feishu.secret",
            "notify.email.smtp_host",
            "notify.email.smtp_port",
            "notify.email.use_ssl",
            "notify.email.username",
            "notify.email.password",
            "notify.email.sender",
            "notify.email.to",
            "notify.events.login",
            "notify.events.login_expired",
            "notify.events.hourly",
            "notify.events.course_completed",
            "notify.events.all_completed",
            "notify.events.video_completed",
            "notify.events.anomaly",
            "notify.events.errors",
        ]
        return {k: self.get(k) for k in keys}


settings_store = SettingsStore()
