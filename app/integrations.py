"""一键配置：把 MCP 服务与技能安装到本机 AI Agent（Codex / Claude Code / WorkBuddy）。"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .client_api import CONFIG_PATH, load_config, save_config

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKILL_SOURCE = PROJECT_ROOT / "skills" / "ykt-helper"

# 各 agent 的技能目录（WorkBuddy 兼容 OpenClaw 技能生态）
CLIENT_DIRS = {
    "codex": Path.home() / ".codex",
    "claude": Path.home() / ".claude",
    "workbuddy": Path.home() / ".workbuddy",
}

MCP_SERVER_MODULE = "app.mcp_server"


def mcp_command() -> list[str]:
    """通过 uv 运行 MCP 服务（stdio）。"""
    return ["uv", "run", "--directory", str(PROJECT_ROOT), "python", "-m", MCP_SERVER_MODULE]


def mcp_env(url: str, token: str) -> dict[str, str]:
    env = {"YKT_URL": url}
    if token:
        env["YKT_TOKEN"] = token
    return env


# ----------------------------------------------------------------------
# MCP 服务注册
# ----------------------------------------------------------------------


def _run(cmd: list[str]) -> tuple[bool, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode == 0, out.strip()
    except FileNotFoundError:
        return False, f"命令不存在：{cmd[0]}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def install_mcp_codex(url: str, token: str) -> list[str]:
    """优先用 `codex mcp add`，失败则直接写 ~/.codex/config.toml（自动备份）。"""
    logs: list[str] = []
    cmd = ["codex", "mcp", "add", "ykt"]
    for k, v in mcp_env(url, token).items():
        cmd += ["--env", f"{k}={v}"]
    cmd += ["--"] + mcp_command()
    ok, out = _run(cmd)
    if ok:
        logs.append("已通过 `codex mcp add` 注册 MCP 服务 ykt")
        return logs
    logs.append(f"`codex mcp add` 未成功（{out[:120]}），改用直接写入 config.toml")
    logs.extend(_write_codex_toml(url, token))
    return logs


def _write_codex_toml(url: str, token: str) -> list[str]:
    path = CLIENT_DIRS["codex"] / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if "[mcp_servers.ykt]" in text:
        return [f"~/.codex/config.toml 中已存在 [mcp_servers.ykt]，跳过（如需修改请先手动删除该段）"]
    backup = path.with_suffix(".toml.bak")
    if path.exists():
        shutil.copy2(path, backup)
    block = ["", "[mcp_servers.ykt]", 'command = "uv"', "args = ["]
    block.append(f'  "run", "--directory", "{PROJECT_ROOT}",')
    block.append(f'  "python", "-m", "{MCP_SERVER_MODULE}",')
    block.append("]")
    block.append("[mcp_servers.ykt.env]")
    block.append(f'YKT_URL = "{url}"')
    if token:
        block.append(f'YKT_TOKEN = "{token}"')
    with path.open("a", encoding="utf-8") as f:
        f.write("\n".join(block) + "\n")
    logs = [f"已写入 {path}（原文件备份为 {backup.name}）"]
    if not token:
        logs.append("未写入 YKT_TOKEN：如服务设置了访问口令，请手动补上")
    return logs


def install_mcp_claude(url: str, token: str) -> list[str]:
    logs: list[str] = []
    cmd = ["claude", "mcp", "add", "ykt"]
    for k, v in mcp_env(url, token).items():
        cmd += ["-e", f"{k}={v}"]
    cmd += ["--"] + mcp_command()
    ok, out = _run(cmd)
    if ok:
        logs.append("已通过 `claude mcp add` 注册 MCP 服务 ykt")
    else:
        logs.append(f"`claude mcp add` 未成功：{out[:160]}")
        logs.append("可手动执行：claude mcp add ykt --env YKT_URL=... -- uv run python -m app.mcp_server")
    return logs


def install_mcp_workbuddy(url: str, token: str) -> list[str]:
    """WorkBuddy 走技能生态（OpenClaw 兼容）；MCP 注册入口依版本而异，这里给出可粘贴配置。"""
    cfg = {
        "mcpServers": {
            "ykt": {
                "command": mcp_command()[0],
                "args": mcp_command()[1:],
                "env": mcp_env(url, token),
            }
        }
    }
    out = CONFIG_PATH.parent / "mcp-workbuddy.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return [
        f"WorkBuddy 以「技能」为主入口，已安装技能（见下）；同时把 MCP 配置写到 {out}",
        "如果 WorkBuddy 设置里有「MCP 服务器 / 连接器」入口，可直接粘贴该 JSON 内容",
    ]


def install_mcp(clients: list[str], url: str, token: str) -> dict:
    result: dict[str, list[str]] = {}
    for client in clients:
        if client == "codex":
            result["codex"] = install_mcp_codex(url, token)
        elif client == "claude":
            result["claude"] = install_mcp_claude(url, token)
        elif client == "workbuddy":
            result["workbuddy"] = install_mcp_workbuddy(url, token)
        else:
            result[client] = [f"未知客户端：{client}"]
    return result


# ----------------------------------------------------------------------
# 技能安装
# ----------------------------------------------------------------------


def install_skill(clients: list[str]) -> dict:
    result: dict[str, list[str]] = {}
    if not SKILL_SOURCE.exists():
        return {"error": [f"技能源目录不存在：{SKILL_SOURCE}"]}
    for client in clients:
        base = CLIENT_DIRS.get(client)
        if base is None:
            result[client] = ["未知客户端"]
            continue
        target_dir = base / "skills"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "ykt-helper"
        try:
            if target.is_symlink() or target.exists():
                if target.is_symlink():
                    target.unlink()
                else:
                    shutil.rmtree(target)
            try:
                target.symlink_to(SKILL_SOURCE, target_is_directory=True)
                result[client] = [f"已软链技能 → {target}（始终跟随仓库最新内容）"]
            except OSError:
                shutil.copytree(SKILL_SOURCE, target)
                result[client] = [f"已复制技能 → {target}"]
        except Exception as e:  # noqa: BLE001
            result[client] = [f"安装失败：{e}"]
    return result


def default_clients() -> list[str]:
    return [name for name, path in CLIENT_DIRS.items() if path.exists()]


def remember_endpoint(url: str, token: str) -> Path:
    return save_config(url=url, token=token)


def current_endpoint() -> dict:
    return load_config()
