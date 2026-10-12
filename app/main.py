"""FastAPI 应用入口。"""
from __future__ import annotations

import html
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api.routes import router as api_router
from .runtime import Runtime

def _resolve_web_dir() -> Path:
    """前端静态目录：源码运行取 app/web；PyInstaller 冻结后取解包目录下的 app/web。"""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
        return base / "app" / "web"
    return Path(__file__).resolve().parent / "web"


WEB_DIR = _resolve_web_dir()

runtime = Runtime()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await runtime.startup()
    try:
        yield
    finally:
        await runtime.shutdown()


app = FastAPI(title="雨课堂网课助手", version=__version__, lifespan=lifespan)
app.state.runtime = runtime


LOGIN_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>雨课堂助手 · 访问口令</title>
<style>
body{background:#0f1117;color:#e6e8ef;font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;
display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
.card{background:#171a23;border:1px solid #262b38;border-radius:16px;padding:36px 32px;width:320px;text-align:center}
h1{font-size:18px;margin:0 0 8px}p{color:#8b93a7;font-size:13px;margin:0 0 20px}
input{width:100%;box-sizing:border-box;background:#0f1117;border:1px solid #2c3140;color:#e6e8ef;
border-radius:10px;padding:10px 14px;font-size:14px;outline:none}
input:focus{border-color:#5b6cff}
button{margin-top:16px;width:100%;background:#5b6cff;border:0;color:#fff;border-radius:10px;
padding:11px 0;font-size:14px;cursor:pointer}
button:hover{background:#4a5bf0}
.err{color:#ff6b6b;font-size:12px;margin-top:10px;min-height:16px}
</style></head>
<body><form class="card" method="post" action="/login">
<h1>雨课堂网课助手</h1><p>请输入看板访问口令</p>
<input type="password" name="token" placeholder="访问口令" autofocus>
<!--NEXT_INPUT-->
<button type="submit">进入看板</button>
<div class="err"><!--ERROR--></div>
</form></body></html>"""


def _login_page(error: str = "", next_url: str = "") -> HTMLResponse:
    safe_next = html.escape(next_url, quote=True)
    next_input = f'<input type="hidden" name="next" value="{safe_next}">' if next_url else ""
    page = LOGIN_HTML.replace("<!--NEXT_INPUT-->", next_input).replace("<!--ERROR-->", html.escape(error))
    return HTMLResponse(page)


@app.middleware("http")
async def access_token_middleware(request: Request, call_next):
    token = (runtime.cfg["server"].get("access_token") or "").strip()
    path = request.url.path
    if not token or path == "/login" or path.startswith("/static"):
        return await call_next(request)
    provided = (
        request.headers.get("x-access-token")
        or request.query_params.get("token")
        or request.cookies.get("access_token")
    )
    if provided != token:
        if path.startswith("/api/"):
            return JSONResponse({"detail": "未授权：请输入访问口令"}, status_code=401)
        return RedirectResponse(f"/login?next={path}")
    return await call_next(request)


@app.get("/login")
async def login_page():
    return _login_page()


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    token = (runtime.cfg["server"].get("access_token") or "").strip()
    if str(form.get("token") or "") != token:
        return _login_page(error="口令不正确", next_url=str(form.get("next") or ""))
    nxt = str(form.get("next") or "/")
    resp = RedirectResponse(nxt if nxt.startswith("/") else "/", status_code=303)
    resp.set_cookie("access_token", token, httponly=True, samesite="lax", max_age=30 * 86400)
    return resp


app.include_router(api_router, prefix="/api")
if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(WEB_DIR / "index.html"))


@app.get("/favicon.ico")
async def favicon():
    icon = WEB_DIR / "favicon.svg"
    if icon.exists():
        return FileResponse(str(icon), media_type="image/svg+xml")
    return JSONResponse({}, status_code=404)
