"""通知模块：飞书机器人 + 邮件（SMTP）。"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import smtplib
import time
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Any

import httpx

from .db import log_event
from .settings_store import SettingsStore

DEDUP_WINDOW_SEC = 900  # 同一去重键 15 分钟内只通知一次
KEYWORD_PREFIX = "【雨课堂助手】"  # 飞书关键词安全设置可用此关键词


class Notifier:
    def __init__(self, settings: SettingsStore) -> None:
        self.settings = settings
        self._dedup: dict[str, float] = {}

    # ---------------- 对外入口 ----------------

    async def notify(
        self,
        kind: str,
        title: str,
        body: str = "",
        dedup_key: str | None = None,
        force: bool = False,
    ) -> None:
        """按事件类型发送通知（kind 对应 notify.events.<kind> 开关）。"""
        if not force:
            if not self.settings.get_bool("notify.enabled", True):
                return
            if not self.settings.get_bool(f"notify.events.{kind}", True):
                return
        if dedup_key:
            last = self._dedup.get(dedup_key, 0)
            if time.time() - last < DEDUP_WINDOW_SEC:
                return
            self._dedup[dedup_key] = time.time()

        text = f"{title}\n{body}".strip()
        results = await self._dispatch(text)
        ok = any(r.get("ok") for r in results.values() if r)
        log_event(
            "success" if ok else "warning",
            "notify",
            f"通知[{kind}] {title}" + ("" if ok else self._describe_failure(results)),
            {"channels": results},
        )

    async def send_test(self) -> dict[str, Any]:
        """给所有已配置的渠道发一条测试消息，返回每个渠道的结果与原因。"""
        text = "这是一条测试通知，如果你看到了它，说明通知配置成功。"
        return await self._dispatch(text)

    @staticmethod
    def _describe_failure(results: dict[str, Any]) -> str:
        parts = [f"{name}: {r.get('error')}" for name, r in results.items() if r and not r.get("ok")]
        if parts:
            return "（" + "；".join(parts) + "）"
        return "（未配置任何通知渠道）"

    # ---------------- 具体渠道 ----------------

    async def _dispatch(self, text: str) -> dict[str, Any]:
        results: dict[str, Any] = {}
        tasks = []
        webhook = (self.settings.get("notify.feishu.webhook") or "").strip()
        secret = (self.settings.get("notify.feishu.secret") or "").strip()
        if webhook:
            tasks.append(("feishu", self._send_feishu(webhook, secret, text)))
        email_to = self.settings.get("notify.email.to") or []
        smtp_host = (self.settings.get("notify.email.smtp_host") or "").strip()
        if smtp_host and email_to:
            tasks.append(("email", self._send_email(text)))

        if not tasks:
            return results
        done = await asyncio.gather(*[t[1] for t in tasks], return_exceptions=True)
        for (name, _), res in zip(tasks, done):
            if isinstance(res, Exception):
                results[name] = {"ok": False, "error": str(res)}
                log_event("error", "notify", f"{name} 通知发送失败: {res}")
            else:
                results[name] = {"ok": True}
        return results

    async def _send_feishu(self, webhook: str, secret: str, text: str) -> None:
        # 统一带上关键词，便于使用飞书「自定义关键词」安全设置（关键词填：雨课堂助手）
        text = f"{KEYWORD_PREFIX}{text}"
        payload: dict[str, Any] = {"msg_type": "text", "content": {"text": text}}
        if secret:
            ts = str(int(time.time()))
            string_to_sign = f"{ts}\n{secret}"
            # 注意：飞书的算法是把 string_to_sign 作为 HMAC 的 key、消息体留空，
            # 与常见"secret 作为 key"的写法相反，写反会一直报 19021 签名校验失败。
            sign = base64.b64encode(
                hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
            ).decode("utf-8")
            payload["timestamp"] = ts
            payload["sign"] = sign
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(webhook, json=payload)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") not in (0, None):
                raise RuntimeError(f"飞书返回错误: {data}")

    def _send_email_sync(self, text: str) -> None:
        host = (self.settings.get("notify.email.smtp_host") or "").strip()
        port = self.settings.get_int("notify.email.smtp_port", 465)
        use_ssl = self.settings.get_bool("notify.email.use_ssl", True)
        username = (self.settings.get("notify.email.username") or "").strip()
        password = self.settings.get("notify.email.password") or ""
        sender = (self.settings.get("notify.email.sender") or "").strip() or username
        to_list = self.settings.get("notify.email.to") or []
        if isinstance(to_list, str):
            to_list = [x.strip() for x in to_list.split(",") if x.strip()]

        msg = MIMEText(text, "plain", "utf-8")
        msg["Subject"] = Header("雨课堂助手通知", "utf-8")
        msg["From"] = formataddr((str(Header("雨课堂助手", "utf-8")), sender))
        msg["To"] = ", ".join(to_list)

        if use_ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=20)
        else:
            server = smtplib.SMTP(host, port, timeout=20)
            server.starttls()
        try:
            if username:
                server.login(username, password)
            server.sendmail(sender, to_list, msg.as_string())
        finally:
            try:
                server.quit()
            except Exception:
                pass

    async def _send_email(self, text: str) -> None:
        await asyncio.to_thread(self._send_email_sync, text)
