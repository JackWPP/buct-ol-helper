"""独立运行入口：内置服务（桌面端）与 CLI 共用。

用法：
    ykt-server                 # 等价于 --serve：启动看板服务（桌面端内置调用）
    ykt-server --serve         # 同上
    ykt-server status --json   # 等价于 `ykt status --json`（CLI）

端口策略（解决端口占用问题）：
    优先绑定 YKT_PORT / 配置里的端口；若已被占用，则绑定端口 0 交给系统分配，
    并在绑定成功后把**真实端口**打到 stdout（`[ykt] LISTEN <port>`），
    由桌面端读取后连接 —— 不做任何端口猜测，也就不会连错到别的服务。

被 PyInstaller 冻结时：
    * 数据目录默认落在用户数据目录，避免写进只读的 App 包；
    * 支持 YKT_PARENT_PID：父进程（桌面端）退出后自动退出，防止残留僵尸服务。
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

APP_NAME = "雨课堂网课助手"


def user_data_dir() -> Path:
    """各平台标准的应用数据目录（与 Electron 的 userData 约定保持一致）。"""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / APP_NAME
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home()) / APP_NAME
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / "ykt-helper"
    return base / "data"


def _watch_parent(ppid: int) -> None:
    """兜底：父进程 PID 消失则自杀（注意父进程变僵尸时 PID 仍存在，故仅作兜底）。"""
    while True:
        time.sleep(15)
        try:
            os.kill(ppid, 0)
        except OSError:
            os._exit(0)


def _watch_stdin() -> None:
    """主力机制：桌面端退出时管道关闭 → stdin 立即读到 EOF → 自杀。

    相比轮询 PID，这种方式在父进程被强杀 / 变僵尸时都能可靠触发。
    """
    try:
        while True:
            chunk = sys.stdin.buffer.readline()
            if not chunk:
                break
    except Exception:
        pass
    os._exit(0)


def bootstrap() -> None:
    if getattr(sys, "frozen", False):
        os.environ.setdefault("YKT_DATA_DIR", str(user_data_dir()))
    ppid = os.environ.get("YKT_PARENT_PID")
    if ppid and ppid.isdigit():
        threading.Thread(target=_watch_parent, args=(int(ppid),), daemon=True).start()
        if sys.stdin is not None:
            threading.Thread(target=_watch_stdin, daemon=True).start()


def pick_port(host: str, preferred: int) -> int:
    """优先用 preferred；被占用则返回 0（交给系统分配空闲端口）。

    用真实 bind 试探：Python 默认不设 SO_REUSEADDR，能准确识别
    “别的进程绑在 0.0.0.0 上占用了 127.0.0.1” 这类情况。
    """
    if not preferred:
        return 0
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, preferred))
        except OSError:
            print(f"[ykt] 端口 {preferred} 已被占用，改用系统分配的空闲端口", flush=True)
            return 0
    return preferred


def serve() -> None:
    import uvicorn

    from app.config import load_config
    from app.main import app as fastapi_app

    class _Server(uvicorn.Server):
        """绑定成功后把真实端口打到 stdout，供桌面端读取。"""

        def __init__(self, config: "uvicorn.Config") -> None:
            super().__init__(config)
            self.bound_port = 0

        async def startup(self, sockets=None):  # type: ignore[override]
            await super().startup(sockets)
            for server in self.servers:
                for sock in server.sockets or []:
                    try:
                        self.bound_port = int(sock.getsockname()[1])
                    except Exception:
                        pass
            print(f"[ykt] LISTEN {self.bound_port}", flush=True)

    cfg = load_config(os.environ.get("YKT_CONFIG"))
    # 冻结二进制（桌面端内置 / 独立运行）默认只听本地回环，避免无意暴露到局域网；
    # 需要对外服务请显式设置 YKT_HOST=0.0.0.0（Docker 内部使用 run.py，不受影响）。
    if getattr(sys, "frozen", False):
        host = os.environ.get("YKT_HOST") or "127.0.0.1"
    else:
        host = os.environ.get("YKT_HOST") or cfg["server"].get("host", "127.0.0.1")
    preferred = int(os.environ.get("YKT_PORT") or cfg["server"].get("port", 8765))
    port = pick_port(host, preferred)

    print(f"[ykt] 看板服务启动中：{host}:{port or '自动分配'}", flush=True)
    config = uvicorn.Config(fastapi_app, host=host, port=port, log_level="info", access_log=False)
    _Server(config).run()


def main(argv: list[str] | None = None) -> int:
    bootstrap()
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("--serve", "serve", "--server"):
        serve()
        return 0
    from app.cli import main as cli_main

    return cli_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
