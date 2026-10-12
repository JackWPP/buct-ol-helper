"""运行时容器：账号会话、客户端、引擎、登录、后台任务。"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from .config import data_dir, get_config
from .db import db_session, get_or_create_account, init_db, log_event
from .engine.manager import WatchManager
from .jobs import refresh_progress, sync_courses
from .models import Account, Course
from .notify import Notifier
from .settings_store import settings_store
from .yuketang.auth import AuthManager
from .yuketang.client import AccountSession, ApiError, SessionExpired, YktClient, extract_identity


class Runtime:
    def __init__(self) -> None:
        self.cfg = get_config()
        self.settings = settings_store
        self.base_url = (self.cfg["account"].get("base_url") or "").rstrip("/")
        self.account_id: int | None = None

        self.session = AccountSession(self.base_url)
        self.client = YktClient(self.session, self.settings)
        self.notifier = Notifier(self.settings)
        self.manager = WatchManager(
            self.client,
            self.settings,
            self.notifier,
            on_session_expired=self._on_session_expired,
            on_uv_id=self.set_uv_id,
        )
        self.auth = AuthManager(
            self.base_url,
            self.install_login,
            timeout=self.settings.get_float("engine.request_timeout", 15.0),
        )
        self.jobs: dict[str, dict[str, Any]] = {
            "sync": {"running": False, "message": "", "last_at": None, "result": None},
            "progress": {"running": False, "message": "", "last_at": None, "result": None},
        }
        self.started_at = time.time()
        self._bg_task: asyncio.Task | None = None
        self._engine_was_running = False
        self._expired_marked_at = 0.0
        self._had_session = False
        self._last_report_snapshot: tuple | None = None

    # ------------------------------------------------------------------
    # 启动 / 关闭
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        init_db()
        self.settings.load()
        acct = get_or_create_account(self.base_url, self.cfg["account"].get("name") or "我的账号")
        self.account_id = acct.id
        if acct.session_json:
            try:
                self.client.session = AccountSession.from_dict(json.loads(acct.session_json))
            except Exception:
                pass
        elif (data_dir() / "session.json").exists():
            # 数据库被重建时的兜底：从本地会话文件恢复登录态
            try:
                self.client.session = AccountSession.from_dict(
                    json.loads((data_dir() / "session.json").read_text(encoding="utf-8"))
                )
                self._persist_session()
            except Exception:
                pass
        self._had_session = bool(self.client.session.cookies.get("sessionid"))
        # 补一次 csrftoken
        if self.client.session.cookies.get("sessionid"):
            try:
                await self.client.refresh_csrf()
            except Exception:
                pass
        log_event(
            "info",
            "system",
            f"服务已启动（{self.base_url}，登录状态：{'已登录' if self.client.session.cookies.get('sessionid') else '未登录'}）",
        )
        self._bg_task = asyncio.create_task(self._background())

    async def shutdown(self) -> None:
        if self._bg_task:
            self._bg_task.cancel()
            try:
                await self._bg_task
            except (asyncio.CancelledError, Exception):
                pass
        await self.manager.shutdown()
        await self.client.close()

    async def _background(self) -> None:
        """后台循环：会话保活、自动回填进度、定时进度汇报。"""
        try:
            await asyncio.sleep(3)
            ok = await self.check_session()
            if ok and self.settings.get_bool("engine.auto_start", False):
                await self.manager.start()
            last_check = time.time()
            last_refresh = time.time()
            last_report = time.time()
            while True:
                await asyncio.sleep(30)
                now = time.time()
                if now - last_check >= 1800:
                    last_check = now
                    await self.check_session()
                if not self.logged_in:
                    continue
                refresh_min = max(2, self.settings.get_int("engine.auto_refresh_minutes", 10))
                if now - last_refresh >= refresh_min * 60 and not self.jobs["progress"]["running"]:
                    last_refresh = now
                    asyncio.create_task(self.run_progress_refresh())
                report_min = max(10, self.settings.get_int("engine.report_interval_minutes", 60))
                if now - last_report >= report_min * 60:
                    last_report = now
                    await self.send_progress_report()
        except asyncio.CancelledError:
            return
        except Exception as e:  # noqa: BLE001
            log_event("error", "system", f"后台任务异常: {type(e).__name__}: {e}")

    async def send_progress_report(self, force: bool = False) -> dict:
        """进度汇报（默认每小时一次，可通过看板/通知发送）。"""
        from .models import Leaf as _Leaf  # 局部导入避免循环依赖

        speed = max(0.5, self.settings.get_float("engine.speed", 1.0))
        parallel = max(1, self.settings.get_int("engine.max_parallel_videos", 2))
        hour_ago = datetime.now() - timedelta(hours=1)
        with db_session() as s:
            courses = s.scalars(select(Course).where(Course.account_id == self.account_id)).all()
            enabled = [c for c in courses if c.enabled]
            total_videos = sum(c.total_videos or 0 for c in enabled)
            done_videos = sum(c.done_videos or 0 for c in enabled)
            total_sec = sum(c.total_seconds or 0 for c in enabled)
            watched_sec = sum(c.watched_seconds or 0 for c in enabled)
            recent_done = len(
                s.scalars(
                    select(_Leaf.id).where(
                        _Leaf.status == "done",
                        _Leaf.last_progress_at.is_not(None),
                        _Leaf.last_progress_at >= hour_ago,
                    )
                ).all()
            )

        remaining = max(0.0, total_sec - watched_sec)
        eta_sec = remaining / speed / max(1, min(parallel, len(enabled) or 1))
        snapshot = (done_videos, round(watched_sec))
        if not force and not self.manager.running and snapshot == self._last_report_snapshot:
            return {"skipped": True, "reason": "无变化且引擎已暂停"}
        self._last_report_snapshot = snapshot

        active = self.manager.live_snapshot()
        lines = [
            f"已启用 {len(enabled)} 门课程 / {total_videos} 个视频",
            f"已完成 {done_videos} 个（{round(done_videos / total_videos * 100, 1) if total_videos else 0}%），"
            f"已看 {watched_sec / 3600:.1f} 小时 / {total_sec / 3600:.1f} 小时",
            f"近 1 小时完成：{recent_done} 个视频",
        ]
        if active:
            for a in active[:3]:
                pos = a.get("position") or 0
                dur = a.get("duration") or 0
                lines.append(
                    f"正在观看：{a.get('course', '')} · {str(a.get('title', ''))[:24]}"
                    f"（{int(pos // 60)}:{int(pos % 60):02d} / {int(dur // 60)}:{int(dur % 60):02d}）"
                )
        else:
            lines.append("当前没有正在观看的视频")
        lines.append(f"预计还需：约 {eta_sec / 3600:.1f} 小时（{speed}x，{min(parallel, len(enabled) or 1)} 路并行）")
        lines.append(f"引擎状态：{'运行中' if self.manager.running else '已暂停'}")

        body = "\n".join(lines)
        if force:
            await self.notifier.notify("hourly", "雨课堂刷课进度汇报", body, force=True)
        else:
            await self.notifier.notify("hourly", "雨课堂刷课进度汇报", body)
        log_event("info", "report", "已发送进度汇报")
        return {"ok": True, "body": body}

    # ------------------------------------------------------------------
    # 会话
    # ------------------------------------------------------------------

    @property
    def logged_in(self) -> bool:
        return bool(self.client.session.cookies.get("sessionid"))

    async def check_session(self) -> bool | None:
        if not self.logged_in:
            self._set_account_status("logged_out")
            return False
        try:
            basic = await self.client.get_basic_info()
        except SessionExpired:
            await self._on_session_expired()
            return False
        except ApiError:
            return None  # 网络问题，保持原状态
        ok = isinstance(basic, dict) and basic.get("code") == 0
        if ok:
            self._set_account_status("active")
            return True
        await self._on_session_expired()
        return False

    async def _on_session_expired(self) -> None:
        was_logged_in = self._had_session
        if not was_logged_in:
            self._set_account_status("logged_out")
            return
        self._had_session = False
        if time.time() - self._expired_marked_at < 1800:
            return
        self._expired_marked_at = time.time()
        self._engine_was_running = self.manager.running or bool(self.manager.course_tasks)
        await self.manager.stop()
        self._set_account_status("expired")
        log_event("warning", "auth", "登录已过期，刷课已暂停，请重新扫码登录")
        await self.notifier.notify(
            "login_expired",
            "雨课堂登录已过期",
            "刷课已自动暂停。请打开看板重新扫码登录，登录后会自动恢复。",
            dedup_key="login_expired",
        )

    async def set_uv_id(self, uv_id: str) -> None:
        """记录学校标识（uv_id / university-id），视频与进度接口需要它。"""
        uv_id = (uv_id or "").strip()
        if not uv_id:
            return
        sess = self.client.session
        if sess.uv_id == uv_id and sess.university_id == uv_id:
            return
        sess.uv_id = uv_id
        if not sess.university_id:
            sess.university_id = uv_id
        self._persist_session()
        log_event("info", "auth", f"已识别学校标识 uv_id={uv_id}")

    def _persist_session(self) -> None:
        sess = self.client.session
        payload = sess.to_dict()
        if self.account_id:
            with db_session() as s:
                acct = s.get(Account, self.account_id)
                if acct is not None:
                    acct.user_id = sess.user_id
                    acct.display_name = sess.display_name
                    acct.university_id = sess.university_id
                    acct.uv_id = sess.uv_id
                    acct.session_json = json.dumps(payload, ensure_ascii=False)
        try:
            (data_dir() / "session.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    def _set_account_status(self, status: str) -> None:
        if self.account_id is None:
            return
        with db_session() as s:
            acct = s.get(Account, self.account_id)
            if acct is not None:
                acct.status = status
                acct.last_check_at = datetime.now()

    # ------------------------------------------------------------------
    # 登录安装（扫码 / Cookie 导入共用）
    # ------------------------------------------------------------------

    async def install_login(self, account: AccountSession) -> dict:
        prev_running = self._engine_was_running or self.manager.running
        prev_session = self.client.session
        account.base_url = self.base_url
        self.client.session = account
        try:
            return await self._install_login_inner(account, prev_running)
        except Exception:
            # 登录失败：回滚会话，避免残留无效凭证
            self.client.session = prev_session
            raise

    async def _install_login_inner(self, account: AccountSession, prev_running: bool) -> dict:
        await self.client.refresh_csrf()
        basic = await self.client.get_basic_info()
        if not (isinstance(basic, dict) and basic.get("code") == 0):
            raise RuntimeError(f"会话校验失败：{json.dumps(basic, ensure_ascii=False)[:200]}")

        userinfo_text = ""
        try:
            userinfo_text = await self.client.get_userinfo()
        except Exception:
            pass
        ident = extract_identity(userinfo_text, basic)

        account.user_id = ident.get("user_id") or account.user_id
        account.display_name = ident.get("display_name") or account.display_name
        cfg_uid = self.cfg["account"].get("university_id")
        cfg_uv = self.cfg["account"].get("uv_id")
        account.university_id = str(cfg_uid) if cfg_uid else (ident.get("university_id") or account.university_id)
        account.uv_id = str(cfg_uv) if cfg_uv else (ident.get("uv_id") or account.uv_id)

        # 持久化（账户行 + 本地会话文件）
        self._persist_session()
        with db_session() as s:
            acct = s.get(Account, self.account_id) if self.account_id else None
            if acct is None:
                acct = Account(base_url=self.base_url, name=self.cfg["account"].get("name") or "我的账号")
                s.add(acct)
                s.flush()
                self.account_id = acct.id
                self._persist_session()
            acct.status = "active"
            acct.last_check_at = datetime.now()

        self._expired_marked_at = 0.0
        self._had_session = True
        await self.notifier.notify(
            "login",
            "雨课堂登录成功",
            f"账号：{account.display_name or account.user_id or '未知'}\n学校标识：{account.university_id or '未探测到'}",
        )
        # 如果之前因过期而中断，自动恢复
        if prev_running:
            await self.manager.start()
        return self.user_public()

    def user_public(self) -> dict:
        sess = self.client.session
        return {
            "user_id": sess.user_id,
            "display_name": sess.display_name,
            "university_id": sess.university_id,
            "uv_id": sess.uv_id,
        }

    async def logout(self) -> None:
        await self.manager.stop()
        self.client.session = AccountSession(self.base_url)
        with db_session() as s:
            acct = s.get(Account, self.account_id) if self.account_id else None
            if acct is not None:
                acct.session_json = None
                acct.status = "logged_out"
        log_event("info", "auth", "已退出登录")

    # ------------------------------------------------------------------
    # 后台任务封装
    # ------------------------------------------------------------------

    async def run_sync(self) -> None:
        state = self.jobs["sync"]
        if state["running"]:
            return
        if not self.logged_in:
            state.update(message="未登录，请先扫码登录")
            log_event("warning", "sync", "同步失败：尚未登录")
            return
        state.update(running=True, message="正在同步课程…")
        try:
            result = await sync_courses(self)
            state.update(result=result, message="同步完成")
        except SessionExpired:
            await self._on_session_expired()
            state.update(message="登录已过期")
        except Exception as e:  # noqa: BLE001
            state.update(message=f"同步失败：{e}")
            log_event("error", "sync", f"同步失败：{type(e).__name__}: {e}")
        finally:
            state.update(running=False, last_at=time.time())

    async def run_progress_refresh(self, course_id: int | None = None) -> None:
        state = self.jobs["progress"]
        if state["running"]:
            return
        if not self.logged_in:
            state.update(message="未登录，请先扫码登录")
            return
        state.update(running=True, message="正在刷新进度…")
        try:
            result = await refresh_progress(self, course_id)
            state.update(result=result, message="进度刷新完成")
        except SessionExpired:
            await self._on_session_expired()
            state.update(message="登录已过期")
        except Exception as e:  # noqa: BLE001
            state.update(message=f"进度刷新失败：{e}")
            log_event("error", "sync", f"进度刷新失败：{type(e).__name__}: {e}")
        finally:
            state.update(running=False, last_at=time.time())
