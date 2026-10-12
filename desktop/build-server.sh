#!/usr/bin/env bash
# 把服务端（FastAPI + 前端静态文件 + Python 运行时）打包成独立二进制。
#
# 产物：desktop/resources/ykt-server/ykt-server（含 _internal/ 运行时目录）
# 用途：桌面端把它打进 App 内，用户无需安装 Python / uv 即可双击使用。
#
# 依赖：uv（会自动以临时依赖方式引入 pyinstaller）
set -euo pipefail

cd "$(dirname "$0")"          # -> desktop/
ROOT="$(cd .. && pwd)"        # 仓库根目录
DIST="$ROOT/desktop/resources"
WORK="$ROOT/build/pyinstaller"

echo "[1/3] 清理旧产物…"
rm -rf "$DIST/ykt-server"
mkdir -p "$DIST" "$WORK"

echo "[2/3] PyInstaller 打包（首次会下载 pyinstaller）…"
cd "$ROOT"
uv run --with pyinstaller --with pyinstaller-hooks-contrib pyinstaller \
  --noconfirm --clean --onedir --console \
  --name ykt-server \
  --distpath "$DIST" \
  --workpath "$WORK" \
  --specpath "$WORK" \
  --collect-submodules app \
  --collect-submodules uvicorn \
  --collect-submodules anyio \
  --add-data "$ROOT/app/web:app/web" \
  --add-data "$ROOT/skills:skills" \
  --hidden-import app.main \
  --hidden-import app.cli \
  --hidden-import app.mcp_server \
  server_entry.py

echo "[3/3] 冒烟测试二进制…"
TMP_DATA="$(mktemp -d)"
# 用 0 号端口：让系统分配，避免机器上恰好占用测试端口导致误报
YKT_DATA_DIR="$TMP_DATA" YKT_PORT=0 YKT_HOST=127.0.0.1 \
  "$DIST/ykt-server/ykt-server" --serve > "$TMP_DATA/boot.log" 2>&1 &
BOOT_PID=$!
ok=0
PORT=""
for _ in $(seq 1 60); do
  sleep 0.5
  PORT="$(grep -o '\[ykt\] LISTEN [0-9]*' "$TMP_DATA/boot.log" 2>/dev/null | tail -1 | awk '{print $3}')"
  if [ -n "$PORT" ] && curl -sf -m 2 "http://127.0.0.1:$PORT/api/system/overview" > /dev/null 2>&1; then
    ok=1
    break
  fi
done
kill "$BOOT_PID" 2>/dev/null || true
wait "$BOOT_PID" 2>/dev/null || true
if [ "$ok" != "1" ]; then
  echo "❌ 冒烟测试失败，启动日志："
  tail -30 "$TMP_DATA/boot.log"
  rm -rf "$TMP_DATA"
  exit 1
fi
echo "    冒烟测试通过（服务端口 $PORT）"
rm -rf "$TMP_DATA"

echo "✅ 完成：$DIST/ykt-server/ykt-server"
du -sh "$DIST/ykt-server"
