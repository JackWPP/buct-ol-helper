"""配置加载：config.yaml + 环境变量覆盖。"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG: dict[str, Any] = {
    "server": {
        "host": "0.0.0.0",
        "port": 8765,
        "access_token": "",
        "data_dir": "data",
    },
    "account": {
        "base_url": "https://grabuct.yuketang.cn",
        "name": "我的账号",
        "university_id": None,
        "uv_id": None,
    },
    "engine": {
        "speed": 1.0,
        "max_parallel_videos": 2,
        "heartbeat_tick": 5.0,
        "flush_interval": 30.0,
        "progress_check_interval": 120.0,
        "retry": 3,
        "request_timeout": 15.0,
        "auto_start": False,
        "auto_select_network_courses": True,  # 同步时自动只勾选"网络课程"
        "auto_refresh_minutes": 10,           # 自动回填平台进度的间隔
        "report_interval_minutes": 60,        # 进度汇报间隔（≥10）
        "active_hours": None,
        "cdn_tag": "bd",
        "dry_run": False,
    },
    "notify": {
        "enabled": True,
        "feishu": {"webhook": "", "secret": ""},
        "email": {
            "smtp_host": "",
            "smtp_port": 465,
            "use_ssl": True,
            "username": "",
            "password": "",
            "sender": "",
            "to": [],
        },
        "events": {
            "login": True,
            "login_expired": True,
            "hourly": True,
            "course_completed": True,
            "all_completed": True,
            "video_completed": False,
            "anomaly": True,
            "errors": True,
        },
    },
}

_config_path: Path | None = None
_config: dict[str, Any] | None = None


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def find_config_path(explicit: str | None = None) -> Path | None:
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    if os.environ.get("YKT_CONFIG"):
        candidates.append(Path(os.environ["YKT_CONFIG"]))
    candidates.append(Path.cwd() / "config.yaml")
    candidates.append(Path(__file__).resolve().parent.parent / "config.yaml")
    for p in candidates:
        if p and p.exists():
            return p
    return None


def load_config(explicit: str | None = None, force: bool = False) -> dict[str, Any]:
    """加载配置（带缓存）。config.yaml 缺失时使用默认值。"""
    global _config, _config_path
    if _config is not None and not force and explicit is None:
        return _config

    path = find_config_path(explicit)
    file_cfg: dict[str, Any] = {}
    if path:
        try:
            file_cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as e:  # pragma: no cover
            print(f"[config] 读取 {path} 失败: {e}")
    cfg = _deep_merge(DEFAULT_CONFIG, file_cfg)

    # 环境变量覆盖
    if os.environ.get("YKT_BASE_URL"):
        cfg["account"]["base_url"] = os.environ["YKT_BASE_URL"]
    if os.environ.get("YKT_ACCESS_TOKEN") is not None and os.environ.get("YKT_ACCESS_TOKEN") != "":
        cfg["server"]["access_token"] = os.environ["YKT_ACCESS_TOKEN"]
    if os.environ.get("YKT_DATA_DIR"):
        cfg["server"]["data_dir"] = os.environ["YKT_DATA_DIR"]
    if os.environ.get("YKT_PORT"):
        cfg["server"]["port"] = int(os.environ["YKT_PORT"])

    _config = cfg
    _config_path = path
    return cfg


def get_config() -> dict[str, Any]:
    return load_config()


def data_dir() -> Path:
    cfg = get_config()
    d = Path(cfg["server"]["data_dir"])
    if not d.is_absolute():
        d = Path.cwd() / d
    d.mkdir(parents=True, exist_ok=True)
    return d
