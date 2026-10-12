"""微信扫码登录与会话导入。

登录流程（与网页端一致）：
1. 访问 /v/course_meta/user_info 获取 csrftoken
2. POST /api/v3/user/login/wechat-auth-param 获取 appId / state / redirectUri
3. 跳转微信开放平台 qrconnect 获取 uuid，拉取二维码图片
4. 轮询 lp.open.weixin.qq.com 获取扫码状态（408 未扫 / 404 已扫待确认 / 405 成功 / 402 过期）
5. 携带回调 code 访问 redirectUri，拿到 sessionid
"""
from __future__ import annotations

import asyncio
import base64
import re
import time
import uuid as uuidlib
from typing import Any, Awaitable, Callable

import httpx

from ..db import log_event
from . import constants as C
from .client import AccountSession

QR_TTL_SEC = 300  # 二维码会话保留时长


class QrLoginSession:
    def __init__(
        self,
        key: str,
        appid: str,
        state: str,
        redirect_uri: str,
        qr_uuid: str,
        qr_data_url: str,
        client: httpx.AsyncClient,
    ):
        self.key = key
        self.appid = appid
        self.state = state
        self.redirect_uri = redirect_uri
        self.qr_uuid = qr_uuid
        self.qr_data_url = qr_data_url
        self.client = client
        self.created_at = time.time()
        self.status = "waiting"  # waiting / scanned / confirmed / expired / error
        self.message = "等待扫码"
        self.error_count = 0
        self.poll_task: asyncio.Task | None = None
        self.user: dict | None = None


class AuthManager:
    def __init__(
        self,
        base_url: str,
        on_login: Callable[[AccountSession], Awaitable[dict]],
        timeout: float = 15.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.on_login = on_login
        self.timeout = timeout
        self.sessions: dict[str, QrLoginSession] = {}

    # ---------------- 二维码登录 ----------------

    async def create_qr(self) -> dict:
        self._gc()
        # 微信状态接口是长轮询，可能长时间挂起，这里单独放宽超时
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(max(40.0, self.timeout), connect=max(15.0, self.timeout)),
            follow_redirects=True,
        )
        try:
            # 1. csrftoken
            await client.get(f"{self.base_url}{C.PATH_USER_INFO_PAGE}")

            # 2. 微信登录参数
            resp = await client.post(
                f"{self.base_url}{C.PATH_WECHAT_AUTH_PARAM}",
                headers={
                    "Content-Type": "application/json",
                    "x-client": "web",
                    "xtbz": "cloud",
                    "Referer": f"{self.base_url}/pro/login",
                    "User-Agent": C.UA,
                },
                json={},
            )
            payload = resp.json()
            data = payload.get("data") or {}
            appid = data.get("appId")
            state = data.get("state")
            redirect_uri = data.get("redirectUri")
            if not (appid and state and redirect_uri):
                raise RuntimeError(f"登录参数获取失败: {str(payload)[:200]}")

            # 3. 换取 uuid 并下载二维码
            resp = await client.get(
                C.WX_QRCONNECT,
                params={
                    "appid": appid,
                    "scope": "snsapi_login",
                    "state": state,
                    "redirect_uri": redirect_uri,
                },
                headers={"User-Agent": C.UA},
            )
            m = re.search(r"uuid=([a-zA-Z0-9]+)", resp.text) or re.search(r'"uuid"\s*:\s*"([a-zA-Z0-9]+)"', resp.text)
            if not m:
                raise RuntimeError("二维码 uuid 解析失败（页面结构可能已更新）")
            qr_uuid = m.group(1)

            img = await client.get(C.WX_QRCODE.format(uuid=qr_uuid), headers={"User-Agent": C.UA})
            if img.status_code != 200 or not img.content:
                raise RuntimeError(f"二维码图片下载失败: HTTP {img.status_code}")
            content = img.content
            # 微信返回的二维码可能是 PNG 或 JPEG，按魔数判断并校验大小
            if content.startswith(b"\x89PNG"):
                mime = "image/png"
            elif content.startswith(b"\xff\xd8\xff"):
                mime = "image/jpeg"
            else:
                raise RuntimeError("二维码图片异常（可能是请求过于频繁被限流），请稍后重试")
            if len(content) < 2000:
                raise RuntimeError("二维码图片异常（可能是请求过于频繁被限流），请稍后重试")
            qr_data_url = f"data:{mime};base64," + base64.b64encode(content).decode()

            key = uuidlib.uuid4().hex
            session = QrLoginSession(key, appid, state, redirect_uri, qr_uuid, qr_data_url, client)
            self.sessions[key] = session
            return {"key": key, "qr_image": qr_data_url, "expires_in": QR_TTL_SEC}
        except Exception:
            await client.aclose()
            raise

    async def poll(self, key: str) -> dict:
        """返回当前扫码状态。

        平台的长轮询接口可能阻塞数十秒，因此这里改为"后台单飞轮询"：
        浏览器可以按任意频率查询，后端始终立即返回最近一次结果，
        同时保证同一时刻只有一个请求打到微信接口（避免请求堆积）。
        """
        session = self.sessions.get(key)
        if session is None:
            return {"status": "expired", "message": "二维码会话不存在，请刷新二维码"}
        if session.status == "confirmed":
            return {"status": "confirmed", "message": "登录成功", "user": session.user}
        if session.status in ("expired", "error"):
            return {"status": session.status, "message": session.message}

        if session.poll_task is None or session.poll_task.done():
            session.poll_task = asyncio.create_task(self._poll_once(session))
        return {"status": session.status, "message": session.message}

    async def _poll_once(self, session: QrLoginSession) -> None:
        try:
            resp = await session.client.get(
                C.WX_POLL,
                params={"uuid": session.qr_uuid, "_": int(time.time() * 1000)},
                headers={"User-Agent": C.UA},
            )
            text = resp.text
            session.error_count = 0
        except Exception:  # 网络抖动/长轮询超时：保留等待态，下一轮继续
            session.error_count += 1
            if session.error_count >= 10:
                session.status = "error"
                session.message = "扫码状态查询失败次数过多，请刷新二维码"
            else:
                session.message = "等待扫码（网络较慢，正在重试…）"
            return

        if "wx_errcode=405" in text:
            m = re.search(r"wx_code='(.*?)'", text) or re.search(r'wx_code="(.*?)"', text)
            if not m:
                session.status = "error"
                session.message = "扫码成功但未取到授权码"
                return
            wx_code = m.group(1)
            try:
                await session.client.get(session.redirect_uri, params={"code": wx_code, "state": session.state})
                sessionid = session.client.cookies.get("sessionid") or self._find_sessionid(session.client)
                if not sessionid:
                    raise RuntimeError("回调未返回 sessionid")
                account = AccountSession(self.base_url, cookies={"sessionid": sessionid})
                user = await self.on_login(account)
                session.status = "confirmed"
                session.message = "登录成功"
                session.user = user
                log_event("success", "auth", f"扫码登录成功：{user.get('display_name') or user.get('user_id')}")
            except Exception as e:  # noqa: BLE001
                session.status = "error"
                session.message = f"登录失败: {e}"
                log_event("error", "auth", f"扫码登录失败: {e}")
            return

        if "wx_errcode=404" in text:
            session.status = "scanned"
            session.message = "已扫码，请在手机上确认"
        elif "wx_errcode=402" in text:
            session.status = "expired"
            session.message = "二维码已过期，请刷新"
        else:  # 408 或其它 → 等待扫码
            session.status = "waiting"
            session.message = "等待扫码（二维码约 5 分钟有效）"

    @staticmethod
    def _find_sessionid(client: httpx.AsyncClient) -> str | None:
        for cookie in client.cookies.jar:
            if cookie.name == "sessionid" and cookie.value:
                return cookie.value
        return None

    # ---------------- Cookie 导入 ----------------

    @staticmethod
    def parse_cookie_string(raw: str) -> dict[str, str]:
        cookies: dict[str, str] = {}
        for part in re.split(r"[;\n]+", raw or ""):
            part = part.strip()
            if not part or "=" not in part:
                continue
            k, v = part.split("=", 1)
            k, v = k.strip(), v.strip().strip('"')
            if k:
                cookies[k] = v
        return cookies

    async def import_cookie_string(self, raw: str) -> dict:
        cookies = self.parse_cookie_string(raw)
        if "sessionid" not in cookies:
            raise RuntimeError("Cookie 中缺少 sessionid，请从浏览器开发者工具复制完整 Cookie")
        account = AccountSession(self.base_url, cookies=cookies)
        user = await self.on_login(account)
        log_event("success", "auth", f"Cookie 导入登录成功：{user.get('display_name') or user.get('user_id')}")
        return user

    # ---------------- 维护 ----------------

    async def _gc_async(self) -> None:
        pass

    def _gc(self) -> None:
        now = time.time()
        dead = [k for k, s in self.sessions.items() if now - s.created_at > QR_TTL_SEC]
        for k in dead:
            s = self.sessions.pop(k)
            try:
                if s.poll_task and not s.poll_task.done():
                    s.poll_task.cancel()
                asyncio.create_task(s.client.aclose())
            except Exception:
                pass
