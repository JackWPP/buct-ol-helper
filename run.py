"""本地/服务器启动入口：python run.py"""
from __future__ import annotations

import os

import uvicorn

from app.config import load_config

if __name__ == "__main__":
    cfg = load_config(os.environ.get("YKT_CONFIG"))
    host = cfg["server"].get("host", "0.0.0.0")
    port = int(os.environ.get("YKT_PORT") or cfg["server"].get("port", 8765))
    uvicorn.run("app.main:app", host=host, port=port, log_level="info")
