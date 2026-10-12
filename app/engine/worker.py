"""视频观看 Worker。

按平台真实前端的行为产生心跳：
- 播放中每 heartbeat_tick 秒产生一条 et="heartbeat" 事件（cp=当前播放位置）；
- 每 flush_interval 秒把累积事件打包 POST 到 /video-log/heartbeat/（单包 ≤50 条）；
- 周期性与平台核对进度（rate），核对失败自动降速、退避；
- 遇到平台风控提示（anomaly）自动暂停冷却并通知。
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import string
import time
from typing import Any

from ..db import db_session, log_event
from ..models import Course, Leaf
from ..yuketang import constants as C
from ..yuketang.client import ApiError, SessionExpired, extract_leaf_fields

DONE = "done"
SKIPPED = "skipped"
FAILED = "failed"
PAUSED = "paused"

# 风控退避冷却（秒），按等级递增
ANOMALY_COOLDOWNS = [60.0, 300.0, 900.0]
MAX_BACKOFF_LEVEL = len(ANOMALY_COOLDOWNS)


def _rand_suffix(n: int = 4) -> str:
    return "".join(random.choice(string.ascii_lowercase) for _ in range(n))


class VideoWorker:
    def __init__(self, manager, course_db_id: int, leaf_db_id: int) -> None:
        self.m = manager
        self.course_db_id = course_db_id
        self.leaf_db_id = leaf_db_id

        self._speed = float(manager.settings.get_float("engine.speed", 1.0))
        self._speed = max(0.5, min(self._speed, 4.0))
        self._backoff_level = 0

    # ------------------------------------------------------------------
    # DB 辅助
    # ------------------------------------------------------------------

    def _load(self) -> tuple[dict | None, dict | None]:
        with db_session() as s:
            leaf = s.get(Leaf, self.leaf_db_id)
            course = s.get(Course, self.course_db_id)
            if leaf is None or course is None:
                return None, None
            leaf_snap = {
                "id": leaf.id,
                "leaf_id": leaf.leaf_id,
                "title": leaf.title,
                "kind": leaf.kind,
                "status": leaf.status,
                "duration": leaf.duration,
                "ccid": leaf.ccid,
                "sku_id": leaf.sku_id,
                "platform_course_id": leaf.platform_course_id,
                "platform_user_id": leaf.platform_user_id,
                "progress": leaf.progress,
            }
            course_snap = {
                "id": course.id,
                "classroom_id": course.classroom_id,
                "name": course.name,
                "sku_id": course.sku_id,
            }
            return leaf_snap, course_snap

    def _update_leaf(self, **fields: Any) -> None:
        from datetime import datetime

        with db_session() as s:
            leaf = s.get(Leaf, self.leaf_db_id)
            if leaf is None:
                return
            for k, v in fields.items():
                setattr(leaf, k, v)
            leaf.last_progress_at = datetime.now()

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    async def run(self) -> str:
        leaf, course = self._load()
        if leaf is None or course is None:
            return FAILED
        # 立刻标记"学习中"并写入实时状态，让看板秒级有反馈（不等网络请求）
        self._update_leaf(status="watching")
        self.m.set_live(
            self.leaf_db_id,
            {
                "leaf_db": self.leaf_db_id,
                "leaf_id": leaf["leaf_id"],
                "title": leaf["title"],
                "course_id": self.course_db_id,
                "course": course["name"],
                "phase": "准备中",
                "position": 0.0,
                "duration": leaf.get("duration") or 0.0,
                "progress": 0.0,
                "speed": self._speed,
                "flushes": 0,
                "events_sent": 0,
                "started_at": time.time(),
            },
        )
        try:
            return await self._run_inner(leaf, course)
        finally:
            self.m.drop_live(self.leaf_db_id)

    async def _run_inner(self, leaf: dict, course: dict) -> str:
        client = self.m.client
        user_id_default = client.session.user_id

        # 1) 获取叶子元信息（同时也用于判定是否视频）
        try:
            info = await client.get_leaf_info(course["classroom_id"], leaf["leaf_id"])
        except SessionExpired:
            raise
        except ApiError as e:
            self._update_leaf(error_msg=f"获取视频信息失败: {e}")
            return FAILED

        if not info:
            self._update_leaf(kind="other", status=SKIPPED, error_msg="非视频内容（无叶子信息）")
            log_event("info", "watch", f"跳过非视频内容：{leaf['title'][:60]}")
            return SKIPPED

        fields = extract_leaf_fields(info)
        if not fields["is_video"]:
            self._update_leaf(kind="other", status=SKIPPED, error_msg="非视频内容")
            log_event("info", "watch", f"跳过非视频内容：{leaf['title'][:60]}")
            return SKIPPED

        # 顺带自愈：leaf_info 会返回学校标识，补上后进度接口才可用
        uv = info.get("university_id")
        if uv and not client.session.uv_id and self.m.on_uv_id:
            await self.m.on_uv_id(str(uv))

        duration = fields["duration"] or leaf["duration"]
        platform_course_id = fields["course_id"] or leaf["platform_course_id"] or course["classroom_id"]
        user_id = fields["user_id"] or leaf["platform_user_id"] or user_id_default
        ccid = fields["ccid"] or leaf["ccid"] or leaf["leaf_id"]
        sku_id = fields["sku_id"] or leaf["sku_id"] or course["sku_id"] or ""
        if not user_id:
            self._update_leaf(error_msg="无法确定 user_id")
            return FAILED

        self._update_leaf(
            kind="video",
            duration=duration,
            ccid=ccid,
            sku_id=sku_id,
            platform_course_id=platform_course_id,
            platform_user_id=user_id,
            status="watching",
            error_msg=None,
        )

        # 2) 进度预检：已完成直接跳过
        try:
            prog = await client.get_video_progress(platform_course_id, user_id, course["classroom_id"], leaf["leaf_id"])
        except SessionExpired:
            raise
        except ApiError:
            prog = None

        # 平台侧的 video_length 是最权威的时长来源
        if prog and prog.get("video_length") and not duration:
            duration = float(prog["video_length"])
            self._update_leaf(duration=duration)

        if prog and prog["completed"]:
            self._update_leaf(
                status=DONE,
                progress=1.0,
                watched_seconds=duration or prog.get("watch_length") or 0.0,
            )
            log_event("info", "watch", f"已完成，跳过：{leaf['title'][:60]}")
            self.m.note_leaf_done(self.course_db_id)
            return DONE

        # 续看位置优先用平台记录的 last_point
        start_pos = 0.0
        if prog:
            if prog.get("last_point"):
                start_pos = max(0.0, float(prog["last_point"]))
            elif duration and prog["rate"] > 0.005:
                start_pos = prog["rate"] * duration
            if duration:
                start_pos = min(duration - 1.0, start_pos)
        wall_budget = ((duration or 3600.0) / max(self._speed, 0.5)) * 2.5 + 600.0

        return await self._watch_loop(
            leaf=leaf,
            course=course,
            platform_course_id=platform_course_id,
            user_id=user_id,
            ccid=ccid,
            sku_id=sku_id,
            duration=duration,
            start_pos=start_pos,
            wall_budget=wall_budget,
        )

    # ------------------------------------------------------------------

    async def _watch_loop(
        self,
        *,
        leaf: dict,
        course: dict,
        platform_course_id: str,
        user_id: str,
        ccid: str,
        sku_id: str,
        duration: float | None,
        start_pos: float,
        wall_budget: float,
    ) -> str:
        client = self.m.client
        settings = self.m.settings
        tick = max(2.0, settings.get_float("engine.heartbeat_tick", 5.0))
        flush_interval = max(10.0, settings.get_float("engine.flush_interval", 30.0))
        check_interval = max(30.0, settings.get_float("engine.progress_check_interval", 120.0))
        dry_run = settings.get_bool("engine.dry_run", False)
        cdn_tag = settings.get("engine.cdn_tag", "bd") or "bd"

        pg = f"{leaf['leaf_id']}_{_rand_suffix()}"
        sq = 0
        events: list[dict] = []

        def make_event(et: str, cp: float, ts_ms: int | None = None) -> dict:
            nonlocal sq
            sq += 1
            return {
                "i": C.HEARTBEAT_DEFAULT_I,
                "et": et,
                "p": C.HEARTBEAT_P,
                "n": cdn_tag,
                "lob": "",
                "cp": round(cp, 1),
                "fp": 0,
                "tp": 0,
                "sp": round(self._speed, 2),
                "ts": str(ts_ms if ts_ms is not None else int(time.time() * 1000)),
                "u": int(user_id) if str(user_id).isdigit() else user_id,
                "uip": "",
                "c": platform_course_id,
                "v": leaf["leaf_id"],
                "skuid": sku_id,
                "classroomid": course["classroom_id"],
                "cc": ccid,
                "d": round(duration or 0.0, 1),
                "pg": pg,
                "sq": sq,
                "t": C.HEARTBEAT_PARAMS_T,
                "cards_id": 0,
                "slide": 0,
                "v_url": "",
            }

        # 初始加载事件序列（只使用平台白名单内的 et，否则会触发 anomaly）
        base_ts = int(time.time() * 1000)
        for idx, et in enumerate(C.EVENTS_START):
            events.append(make_event(et, start_pos, base_ts + idx * 250))

        pos = start_pos
        wall_start = time.monotonic()
        last_flush = time.monotonic()
        last_check = time.monotonic()
        self.flush_count = 0
        self.events_sent = 0
        last_rate: float | None = None
        last_rate_pos: float | None = None
        stall_strikes = 0
        anomaly_notified_level = 0
        anomaly_count = 0
        successful = False

        self._update_leaf(progress=pos / duration if duration else 0.0, watched_seconds=pos)
        self.m.set_live(
            self.leaf_db_id,
            {
                "phase": "观看中",
                "position": round(pos, 1),
                "duration": duration or 0.0,
                "progress": round((pos / duration) if duration else 0.0, 4),
                "watched_seconds": round(pos),
                "speed": round(self._speed, 2),
            },
        )
        log_event("info", "watch", f"开始观看：{leaf['title'][:80]}" + (f"（从 {int(pos)}s 续看）" if pos > 1 else ""))

        while True:
            # 停止 / 暂停检查
            if self.m._shutdown or not self.m.running:
                await self._flush(events, leaf, sku_id, course, user_id, dry_run)
                self._update_leaf(status="pending")
                return PAUSED
            if self.course_db_id in self.m.paused_courses:
                await self._flush(events, leaf, sku_id, course, user_id, dry_run)
                self._update_leaf(status="pending")
                return PAUSED
            if time.monotonic() - wall_start > wall_budget:
                self._update_leaf(error_msg="观看超时（预算耗尽）")
                return FAILED
            if duration and pos >= duration - 0.5:
                break

            # 一个心跳节拍
            sleep_s = tick * random.uniform(0.85, 1.15)
            elapsed = await self.m.interruptible_sleep(sleep_s, self.course_db_id)
            new_pos = pos + elapsed * self._speed
            pos = min(new_pos, duration) if duration else new_pos
            events.append(make_event(C.HEARTBEAT_EVENT, pos))
            self.events_sent += 1

            # 每个节拍都刷新进度（看板粒度更细）+ 实时状态
            cur_progress = (pos / duration) if duration else 0.0
            self._update_leaf(progress=cur_progress, watched_seconds=pos)
            self.m.set_live(
                self.leaf_db_id,
                {
                    "phase": "观看中",
                    "position": round(pos, 1),
                    "duration": duration or 0.0,
                    "progress": round(min(1.0, cur_progress), 4),
                    "watched_seconds": round(pos),
                    "speed": round(self._speed, 2),
                    "events_sent": self.events_sent,
                    "flushes": self.flush_count,
                },
            )

            # 批量上报
            if (time.monotonic() - last_flush >= flush_interval) or len(events) >= C.MAX_EVENTS_PER_FLUSH:
                batch = events
                resp = await self._flush(batch, leaf, sku_id, course, user_id, dry_run)
                events = []
                last_flush = time.monotonic()
                self.flush_count += 1
                self.m.set_live(
                    self.leaf_db_id,
                    {
                        "flushes": self.flush_count,
                        "last_flush_at": time.time(),
                        "last_batch": len(batch),
                        "last_response": (json.dumps(resp, ensure_ascii=False)[:200] if resp else "（已发送）"),
                    },
                )
                action, wait_s = self._inspect_heartbeat_response(resp)
                if action == "anomaly":
                    anomaly_count += 1
                    log_event(
                        "warning",
                        "watch",
                        f"心跳被平台判定异常（第 {anomaly_count} 次）：{json.dumps(resp, ensure_ascii=False)[:220]}",
                    )
                    # 熔断：连续多次异常说明该章节很可能不是可观看视频（或触发风控），及时放弃
                    if anomaly_count >= 3:
                        self._update_leaf(
                            status="failed",
                            error_msg="平台连续返回异常提示，已放弃该章节（可能不是视频内容）",
                        )
                        log_event("warning", "watch", f"连续异常已放弃：{leaf['title'][:60]}")
                        return FAILED
                    if self._backoff_level < MAX_BACKOFF_LEVEL:
                        self._backoff_level += 1
                    if self._backoff_level > anomaly_notified_level:
                        anomaly_notified_level = self._backoff_level
                        await self._notify_anomaly(leaf, "平台心跳返回异常提示（anomaly）")
                    cooldown = ANOMALY_COOLDOWNS[min(self._backoff_level, MAX_BACKOFF_LEVEL) - 1]
                    self._speed = max(0.5, self._speed / 2)
                    await self.m.interruptible_sleep(cooldown, self.course_db_id)
                    pos = await self._resync_pos(platform_course_id, user_id, course, leaf, duration, pos)
                elif action == "limit":
                    await self.m.interruptible_sleep((wait_s or 30.0) + 2.0, self.course_db_id)

            # 进度核对
            if time.monotonic() - last_check >= check_interval:
                last_check = time.monotonic()
                prog = await self._safe_progress(platform_course_id, user_id, course, leaf)
                if prog:
                    if prog["completed"] or prog["rate"] >= 0.999:
                        successful = True
                        break
                    rate = prog["rate"]
                    if last_rate is not None and rate <= last_rate + 0.001 and last_rate_pos is not None and pos > last_rate_pos + max(60.0, check_interval * 0.55):
                        stall_strikes += 1
                        if stall_strikes >= 2:
                            stall_strikes = 0
                            if self._backoff_level < MAX_BACKOFF_LEVEL:
                                self._backoff_level += 1
                            self._speed = max(0.5, self._speed / 2)
                            if self._backoff_level > anomaly_notified_level:
                                anomaly_notified_level = self._backoff_level
                                await self._notify_anomaly(leaf, f"平台进度长时间未增长，已自动降速至 {self._speed}x")
                            cooldown = ANOMALY_COOLDOWNS[min(self._backoff_level, MAX_BACKOFF_LEVEL) - 1]
                            await self.m.interruptible_sleep(cooldown, self.course_db_id)
                            pos = await self._resync_pos(platform_course_id, user_id, course, leaf, duration, pos)
                    else:
                        stall_strikes = 0
                    last_rate = rate
                    last_rate_pos = pos
                    self._update_leaf(
                        progress=min(1.0, rate),
                        watched_seconds=round((duration or 0) * rate) if duration else pos,
                    )

        # 3) 收尾：结束事件 + 确认完成（ended 不在白名单内，正确事件名是 videoend）
        for et in C.EVENTS_END:
            events.append(make_event(et, pos))
        await self._flush(events, leaf, sku_id, course, user_id, dry_run)

        for _ in range(6):
            prog = await self._safe_progress(platform_course_id, user_id, course, leaf)
            if prog and (prog["completed"] or prog["rate"] >= 0.999):
                successful = True
                break
            await self.m.interruptible_sleep(20.0, self.course_db_id)
            if self.m._shutdown or not self.m.running:
                break

        if successful:
            self._update_leaf(status=DONE, progress=1.0, watched_seconds=duration or pos)
            log_event("success", "watch", f"视频完成：{leaf['title'][:80]}")
            self.m.note_leaf_done(self.course_db_id)
            self.m.notifier and await self.m.notifier.notify(
                "video_completed",
                f"视频完成：{leaf['title'][:60]}",
                f"课程：{course['name']}\n已观看至 {int(pos)}s",
                dedup_key=f"video:{leaf['leaf_id']}",
            )
            return DONE

        self._update_leaf(error_msg="已播放完成但平台未记录完成")
        return FAILED

    # ------------------------------------------------------------------

    async def _flush(
        self,
        events: list[dict],
        leaf: dict,
        sku_id: str,
        course: dict,
        user_id: str,
        dry_run: bool,
    ) -> dict | None:
        if not events:
            return None
        if dry_run:
            log_event("info", "watch", f"[dry-run] 将上报 {len(events)} 条心跳（{leaf['title'][:40]}）")
            return None
        batch = events[: C.MAX_EVENTS_PER_FLUSH]
        try:
            await self.m.throttle("heartbeat", 0.6)
            return await self.m.client.post_heartbeat(batch, sku_id, course["classroom_id"], leaf["leaf_id"])
        except SessionExpired:
            raise
        except ApiError as e:
            log_event("warning", "watch", f"心跳上报失败（将重试）：{e}")
            return None

    @staticmethod
    def _inspect_heartbeat_response(resp: dict | None) -> tuple[str | None, float | None]:
        if not resp:
            return None, None
        try:
            text = json.dumps(resp, ensure_ascii=False)
        except Exception:
            text = str(resp)
        low = text.lower()
        if "not login" in low or "未登录" in text:
            raise SessionExpired("心跳响应提示会话失效")
        if "anomaly" in low:
            return "anomaly", None
        m = re.search(r"available in ([0-9.]+)\s*second", text, re.I)
        if m:
            try:
                return "limit", float(m.group(1))
            except ValueError:
                return "limit", 30.0
        return None, None

    async def _safe_progress(self, platform_course_id: str, user_id: str, course: dict, leaf: dict) -> dict | None:
        try:
            await self.m.throttle("progress", 0.3)
            return await self.m.client.get_video_progress(
                platform_course_id, user_id, course["classroom_id"], leaf["leaf_id"]
            )
        except SessionExpired:
            raise
        except ApiError:
            return None

    async def _resync_pos(
        self,
        platform_course_id: str,
        user_id: str,
        course: dict,
        leaf: dict,
        duration: float | None,
        current_pos: float,
    ) -> float:
        prog = await self._safe_progress(platform_course_id, user_id, course, leaf)
        if prog and duration:
            platform_pos = float(prog.get("last_point") or (prog["rate"] * duration))
            # 以平台记录的位置为准（略保守）
            return min(duration - 1.0, max(platform_pos * 0.98, min(current_pos, platform_pos + 60)))
        return current_pos * 0.9

    async def _notify_anomaly(self, leaf: dict, reason: str) -> None:
        log_event("warning", "watch", f"风控自适应：{reason}（{leaf['title'][:50]}）")
        if self.m.notifier:
            await self.m.notifier.notify(
                "anomaly",
                "检测到平台风控信号，已自动降速",
                f"视频：{leaf['title'][:60]}\n原因：{reason}\n当前倍速：{self._speed}x\n已按策略冷却退避，无需人工干预；如频繁出现建议调低倍速。",
                dedup_key=f"anomaly:{leaf['leaf_id']}:{self._backoff_level}",
            )
