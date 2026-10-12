"""刷课调度器：课程级任务编排、并发控制、动态限速。"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from sqlalchemy import select, update

from ..db import db_session, log_event
from ..models import Course, Leaf
from ..yuketang.client import SessionExpired
from .worker import DONE, FAILED, PAUSED, SKIPPED, VideoWorker


class WatchManager:
    def __init__(self, client, settings, notifier, on_session_expired=None, on_uv_id=None) -> None:
        self.client = client
        self.settings = settings
        self.notifier = notifier
        self.on_session_expired = on_session_expired
        self.on_uv_id = on_uv_id

        self.running = False
        self._shutdown = False
        self.paused_courses: set[int] = set()
        self.course_tasks: dict[int, asyncio.Task] = {}
        self._active = 0
        self._slot_cond: asyncio.Condition | None = None
        self._attempts: dict[tuple[int, str], int] = {}
        self._throttles: dict[str, list] = {}
        # 实时状态（内存，供看板秒级展示，不写数据库）
        self.live: dict[int, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self, course_ids: list[int] | None = None) -> list[int]:
        self._shutdown = False
        self.running = True
        ids = course_ids if course_ids is not None else self._enabled_course_ids()
        # 服务重启时可能残留 watching 状态，用一条 UPDATE 快速重置（避免阻塞看板请求）
        try:
            with db_session() as s:
                s.execute(update(Leaf).where(Leaf.status == "watching").values(status="pending"))
        except Exception as e:  # noqa: BLE001
            log_event("warning", "engine", f"重置残留观看状态失败：{e}")
        started = []
        for cid in ids:
            self.paused_courses.discard(cid)
            self._spawn(cid)
            started.append(cid)
        if started:
            log_event("info", "engine", f"引擎启动，共 {len(started)} 门课程")
        else:
            log_event("warning", "engine", "没有勾选任何课程，未启动（请先勾选「参与刷课」）")
        return started

    async def stop(self) -> None:
        self.running = False
        self._mark_courses_paused()
        log_event("info", "engine", "引擎已暂停（进度已保存）")

    async def pause_course(self, course_id: int) -> None:
        self.paused_courses.add(course_id)
        with db_session() as s:
            course = s.get(Course, course_id)
            if course is not None and course.status == "watching":
                course.status = "paused"
        log_event("info", "engine", f"课程 #{course_id} 已暂停")

    def _mark_courses_paused(self) -> None:
        with db_session() as s:
            for course in s.scalars(select(Course).where(Course.status == "watching")).all():
                course.status = "paused"

    async def resume_course(self, course_id: int) -> None:
        self.paused_courses.discard(course_id)
        self.running = True
        self._spawn(course_id)
        log_event("info", "engine", f"课程 #{course_id} 已继续")

    async def shutdown(self) -> None:
        self._shutdown = True
        self.running = False
        for task in list(self.course_tasks.values()):
            task.cancel()
        await asyncio.gather(*self.course_tasks.values(), return_exceptions=True)
        self.course_tasks.clear()

    def poke(self) -> None:
        """参数变更后唤醒等待中的调度（例如提高并发数）。"""

        async def _notify() -> None:
            if self._slot_cond is not None:
                async with self._slot_cond:
                    self._slot_cond.notify_all()

        try:
            asyncio.create_task(_notify())
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 查询辅助
    # ------------------------------------------------------------------

    def _enabled_course_ids(self) -> list[int]:
        with db_session() as s:
            rows = s.scalars(
                select(Course).where(Course.enabled.is_(True)).order_by(Course.order_index, Course.id)
            ).all()
            return [r.id for r in rows]

    def _next_pending_leaf(self, course_id: int) -> dict | None:
        with db_session() as s:
            leaf = s.scalars(
                select(Leaf)
                .where(Leaf.course_id == course_id, Leaf.status == "pending", Leaf.kind.in_(("video", "unknown")))
                .order_by(Leaf.order_index, Leaf.id)
                .limit(1)
            ).first()
            if leaf is None:
                return None
            return {"id": leaf.id, "leaf_id": leaf.leaf_id, "title": leaf.title}

    def note_leaf_done(self, course_id: int) -> None:
        """视频完成后的课程级更新与通知。"""
        stats = refresh_course_stats(course_id)
        if stats and stats.get("just_completed"):
            with db_session() as s:
                course = s.get(Course, course_id)
                name = course.name if course else f"课程#{course_id}"
            log_event("success", "course", f"课程已完成：{name}")
            if self.notifier:
                asyncio.create_task(
                    self.notifier.notify(
                        "course_completed",
                        f"课程刷完啦：{name}",
                        f"共 {stats['total_videos']} 个视频 / {stats['total_hours']:.1f} 小时，已全部完成。",
                        dedup_key=f"course_done:{course_id}",
                    )
                )
            self._check_all_completed()

    def _check_all_completed(self) -> None:
        with db_session() as s:
            courses = s.scalars(select(Course).where(Course.enabled.is_(True))).all()
            if not courses:
                return
            pending = [c for c in courses if c.total_videos > 0 and c.done_videos < c.total_videos]
            if pending:
                return
            names = [c.name for c in courses if c.total_videos > 0]
        if names and self.notifier:
            asyncio.create_task(
                self.notifier.notify(
                    "all_completed",
                    "全部课程已刷完 🎉",
                    "共 %d 门课程：%s" % (len(names), "、".join(names[:10])),
                    dedup_key="all_done",
                )
            )

    # ------------------------------------------------------------------
    # 并发槽位（支持运行时动态调整并发数）
    # ------------------------------------------------------------------

    async def _acquire_slot(self) -> None:
        if self._slot_cond is None:
            self._slot_cond = asyncio.Condition()
        async with self._slot_cond:
            while True:
                limit = max(1, self.settings.get_int("engine.max_parallel_videos", 2))
                if self._active < limit:
                    self._active += 1
                    return
                try:
                    await asyncio.wait_for(self._slot_cond.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass

    async def _release_slot(self) -> None:
        async with self._slot_cond:
            self._active = max(0, self._active - 1)
            self._slot_cond.notify(1)

    @asynccontextmanager
    async def _slot(self) -> AsyncIterator[None]:
        await self._acquire_slot()
        try:
            yield
        finally:
            await self._release_slot()

    # ------------------------------------------------------------------
    # 节流（全局请求最小间隔）
    # ------------------------------------------------------------------

    async def throttle(self, name: str, min_interval: float) -> None:
        entry = self._throttles.setdefault(name, [asyncio.Lock(), 0.0])
        lock: asyncio.Lock = entry[0]
        async with lock:
            now = time.monotonic()
            wait = min_interval - (now - entry[1])
            if wait > 0:
                await asyncio.sleep(wait)
            entry[1] = time.monotonic()

    # ------------------------------------------------------------------
    # 播放器唤醒用睡眠
    # ------------------------------------------------------------------

    async def interruptible_sleep(self, seconds: float, course_id: int | None = None) -> float:
        """可被暂停/停止打断的睡眠，返回实际睡眠秒数。"""
        start = time.monotonic()
        end = start + seconds
        while True:
            if self._shutdown or not self.running:
                return time.monotonic() - start
            if course_id is not None and course_id in self.paused_courses:
                return time.monotonic() - start
            remaining = end - time.monotonic()
            if remaining <= 0:
                return time.monotonic() - start
            await asyncio.sleep(min(0.5, remaining))

    async def wait_ready(self, course_id: int) -> bool:
        """等待引擎运行且课程未暂停；返回 False 表示已关闭。"""
        while True:
            if self._shutdown:
                return False
            if self.running and course_id not in self.paused_courses:
                return True
            await asyncio.sleep(0.4)

    # ------------------------------------------------------------------
    # 课程任务
    # ------------------------------------------------------------------

    def _spawn(self, course_id: int) -> None:
        task = self.course_tasks.get(course_id)
        if task is None or task.done():
            self.course_tasks[course_id] = asyncio.get_event_loop().create_task(self._run_course(course_id))

    async def _run_course(self, course_id: int) -> None:
        try:
            with db_session() as s:
                course = s.get(Course, course_id)
                if course is None:
                    return
                course.status = "watching"
            while True:
                if self._shutdown:
                    break
                if not await self.wait_ready(course_id):
                    break
                leaf = self._next_pending_leaf(course_id)
                if leaf is None:
                    break
                async with self._slot():
                    try:
                        result = await VideoWorker(self, course_id, leaf["id"]).run()
                    except SessionExpired:
                        await self._handle_session_expired()
                        return
                if result in (DONE, SKIPPED):
                    self._attempts.pop((course_id, leaf["leaf_id"]), None)
                    continue
                if result in (PAUSED,):
                    continue
                if result == FAILED:
                    key = (course_id, leaf["leaf_id"])
                    self._attempts[key] = self._attempts.get(key, 0) + 1
                    retry_limit = max(1, self.settings.get_int("engine.retry", 3))
                    if self._attempts[key] >= retry_limit:
                        with db_session() as s:
                            row = s.get(Leaf, leaf["id"])
                            if row is not None:
                                row.status = "failed"
                        log_event("error", "watch", f"视频多次失败已放弃：{leaf['title'][:60]}（可在看板手动重试）")
                        if self.notifier:
                            await self.notifier.notify(
                                "errors",
                                "有视频刷取失败",
                                f"{leaf['title'][:60]} 已重试 {self._attempts[key]} 次仍未完成，稍后可在看板手动重试。",
                                dedup_key=f"leaf_fail:{leaf['leaf_id']}",
                            )
                    else:
                        await asyncio.sleep(5)
                    continue
            self._finalize_course(course_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log_event("error", "engine", f"课程任务异常: {type(e).__name__}: {e}")
        finally:
            self.course_tasks.pop(course_id, None)

    def _finalize_course(self, course_id: int) -> None:
        stats = refresh_course_stats(course_id)
        if stats and stats.get("just_completed"):
            self._check_all_completed()

    async def _handle_session_expired(self) -> None:
        self.running = False
        if self.on_session_expired:
            await self.on_session_expired()

    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "active_videos": self._active,
            "paused_courses": sorted(self.paused_courses),
            "course_tasks": len(self.course_tasks),
        }

    # ------------------------------------------------------------------
    # 实时状态（内存）
    # ------------------------------------------------------------------

    def set_live(self, key: int, fields: dict[str, Any]) -> None:
        entry = self.live.setdefault(key, {})
        entry.update(fields)
        entry["updated_at"] = time.time()

    def drop_live(self, key: int) -> None:
        self.live.pop(key, None)

    def live_snapshot(self) -> list[dict[str, Any]]:
        now = time.time()
        out = []
        for entry in self.live.values():
            item = dict(entry)
            item["stale"] = (now - item.get("updated_at", now)) > 30
            item["idle_sec"] = round(now - item.get("updated_at", now), 1)
            out.append(item)
        out.sort(key=lambda x: x.get("updated_at", 0), reverse=True)
        return out


# ----------------------------------------------------------------------
# 课程统计刷新（sync / worker / 手动重试共用）
# ----------------------------------------------------------------------

def refresh_course_stats(course_id: int) -> dict[str, Any] | None:
    with db_session() as s:
        course = s.get(Course, course_id)
        if course is None:
            return None
        leaves = s.scalars(select(Leaf).where(Leaf.course_id == course_id)).all()
        videos = [l for l in leaves if l.kind == "video"]
        done = [l for l in videos if l.status == "done"]
        was_done = course.status == "done"

        course.total_videos = len(videos)
        course.done_videos = len(done)
        course.total_seconds = round(sum((l.duration or 0.0) for l in videos), 1)
        watched = 0.0
        for l in videos:
            if l.status == "done":
                watched += l.duration or 0.0
            else:
                watched += (l.duration or 0.0) * min(1.0, l.progress or 0.0) if l.duration else l.watched_seconds or 0.0
        course.watched_seconds = round(watched, 1)

        now_done = bool(videos) and len(done) == len(videos)
        if now_done:
            course.status = "done"
        elif any(l.status == "watching" for l in videos):
            course.status = "watching"
        elif course.status == "done" and not now_done:
            course.status = "idle"

        return {
            "course_id": course_id,
            "total_videos": len(videos),
            "done_videos": len(done),
            "total_seconds": course.total_seconds,
            "watched_seconds": course.watched_seconds,
            "total_hours": (course.total_seconds or 0) / 3600.0,
            "just_completed": now_done and not was_done,
        }
