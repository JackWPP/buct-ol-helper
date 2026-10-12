"""后台任务：课程同步、进度刷新。"""
from __future__ import annotations

import asyncio
import time as time_module
from datetime import datetime

from sqlalchemy import select

from .db import db_session, log_event
from .engine.manager import refresh_course_stats
from .models import Course, Leaf
from .yuketang import constants as C
from .yuketang.client import (
    ApiError,
    SessionExpired,
    extract_leaf_fields,
    normalize_duration_seconds,
)

# 单次刷新最多补充多少个视频的时长（避免请求过多）
ENRICH_DURATION_LIMIT = 60
# 单次刷新最多核对多少个已有进度的叶子
PROGRESS_CHECK_LIMIT = 200


def _guess_kind(raw_type, title: str) -> str:
    # leaf_type: 0=视频, 3=推荐阅读, 4=讨论, 5=考试, 6=作业
    if raw_type in (0, "0", "video"):
        return "video"
    if raw_type in C.NON_VIDEO_LEAF_TYPES:
        return "other"
    for kw in C.NON_VIDEO_TITLE_KEYWORDS:
        if kw in (title or ""):
            return "other"
    return "unknown"


def _pick_duration(item: dict) -> float | None:
    for key in ("duration", "video_duration", "leaf_duration", "media_duration", "length"):
        if item.get(key) not in (None, "", 0):
            d = normalize_duration_seconds(item.get(key))
            if d:
                return d
    return None


async def sync_courses(runtime) -> dict:
    """从平台同步课程与章节列表。"""
    client = runtime.client
    log_event("info", "sync", "开始同步课程列表…")
    result = {"courses": 0, "leaves": 0, "errors": []}

    course_list = await client.get_courses()
    payloads: list[dict] = []
    for c in course_list:
        sku = None
        sign = None
        leaves_raw: list[dict] = []
        try:
            cls = await client.get_classroom(c["classroom_id"])
            sku_val = cls.get("free_sku_id") or cls.get("sku_id")
            sku = str(sku_val) if sku_val else None
            if cls.get("course_sign"):
                sign = str(cls["course_sign"])
            # 顺带探测学校标识（视频接口的 uv_id / university_id 必需）
            if cls.get("uv_id") and not runtime.client.session.uv_id:
                await runtime.set_uv_id(str(cls["uv_id"]))
            if sku:
                leaves_raw = await client.get_leaf_level_infos(sku)
        except SessionExpired:
            raise
        except ApiError as e:
            result["errors"].append(f"{c['name']}: {e}")
        teacher = (c.get("teacher") or "").strip()
        is_network = any(kw in teacher for kw in C.NETWORK_COURSE_KEYWORDS)
        payloads.append(
            {"course": c, "sku": sku, "sign": sign, "leaves": leaves_raw, "is_network": is_network}
        )
        await asyncio.sleep(0.25)  # 温和请求

    auto_select = runtime.settings.get_bool("engine.auto_select_network_courses", True)
    course_ids: list[int] = []
    with db_session() as s:
        for idx, item in enumerate(payloads):
            c = item["course"]
            sku = item["sku"]
            leaves_raw = item["leaves"]
            row = s.scalars(
                select(Course).where(Course.account_id == runtime.account_id, Course.classroom_id == c["classroom_id"])
            ).first()
            if row is None:
                row = Course(account_id=runtime.account_id, classroom_id=c["classroom_id"], enabled=False, order_index=idx)
                s.add(row)
                s.flush()
            row.name = c["name"] or row.name
            row.teacher = c["teacher"] or row.teacher
            row.cover_url = c["cover"] or row.cover_url
            row.term = c["term"] or row.term
            row.order_index = idx
            row.sku_id = sku or row.sku_id
            row.course_sign = item["sign"] or row.course_sign
            row.is_network = bool(item["is_network"])
            row.last_sync_at = datetime.now()

            existing = {l.leaf_id: l for l in s.scalars(select(Leaf).where(Leaf.course_id == row.id)).all()}
            chapter_counts: dict[str, int] = {}
            for order, item in enumerate(leaves_raw):
                lid = str(item.get("id") or "")
                if not lid:
                    continue
                raw_title = str(item.get("leaf_level_title") or "").strip()
                chapter = str(item.get("leaf_chapter_title") or "").strip()
                # 视频叶子的 leaf_level_title 常常就是 "Video"，用章节名 + 序号更可读
                if raw_title.lower() in ("", "video", "视频"):
                    chapter_counts[chapter] = chapter_counts.get(chapter, 0) + 1
                    title = f"{chapter} · 视频{chapter_counts[chapter]}" if chapter else f"第 {order + 1} 节"
                else:
                    title = raw_title
                raw_type = item.get("leaf_type")
                kind = _guess_kind(raw_type, title)
                duration = _pick_duration(item)
                hint = C.LEAF_TYPE_HINT.get(str(raw_type))
                skip_msg = (
                    f"{hint}：需到平台手动完成"
                    if hint
                    else "非视频内容（无需观看）"
                )
                leaf = existing.get(lid)
                if leaf is None:
                    leaf = Leaf(
                        course_id=row.id,
                        leaf_id=lid,
                        title=title,
                        kind=kind,
                        kind_hint=hint,
                        order_index=order,
                        duration=duration,
                        raw_leaf_type=str(raw_type) if raw_type is not None else None,
                        status="skipped" if kind == "other" else "pending",
                        error_msg=skip_msg if kind == "other" else None,
                        meta_json=None,
                    )
                    s.add(leaf)
                    result["leaves"] += 1
                else:
                    leaf.title = title
                    leaf.order_index = order
                    leaf.kind_hint = hint
                    if raw_type is not None:
                        leaf.raw_leaf_type = str(raw_type)
                    if kind != "unknown":
                        # 允许按平台数据修正分类（非视频 → 标记跳过，避免误刷）
                        leaf.kind = kind
                        if kind == "other" and leaf.status in ("pending", "failed", "watching"):
                            leaf.status = "skipped"
                            leaf.error_msg = skip_msg
                    if duration and not leaf.duration:
                        leaf.duration = duration
            result["courses"] += 1
            course_ids.append(row.id)

    for cid in course_ids:
        refresh_course_stats(cid)

    # 按规则自动勾选：只刷"网络课程"（且确实有视频），其余课程不参与自动刷课
    if auto_select and course_ids:
        with db_session() as s:
            rows = s.scalars(select(Course).where(Course.account_id == runtime.account_id)).all()
            picked = 0
            for row in rows:
                should = bool(row.is_network) and (row.total_videos or 0) > 0
                row.enabled = should
                if should:
                    picked += 1
        result["auto_enabled"] = picked
        log_event("info", "sync", f"已自动勾选网络课程 {picked} 门（非网络课程不参与，可手动改）")

    msg = f"同步完成：{result['courses']} 门课程，新增 {result['leaves']} 个章节"
    if result["errors"]:
        msg += f"（{len(result['errors'])} 处异常）"
    log_event("success" if not result["errors"] else "warning", "sync", msg)
    return result


async def refresh_progress(runtime, course_id: int | None = None) -> dict:
    """刷新平台上记录的视频进度（以平台数据为准回填）。

    策略：
    1. 先补充未知时长（有界数量，顺带识别非视频内容）；
    2. 再核对"已经动过"的叶子（学习中/已完成/失败/有进度）的进度；
       —— 从未开始过的叶子不在这里查询，观看前 Worker 会单独核对，避免几百次无效请求。
    """
    client = runtime.client
    log_event("info", "sync", "开始刷新平台进度…")
    result = {"checked": 0, "completed": 0, "updated": 0, "errors": 0, "enriched": 0}

    # 1) 先补时长
    result["enriched"] = await _enrich_durations(runtime, course_id)

    # 2) 再核对有进度的叶子
    with db_session() as s:
        q = (
            select(Leaf, Course)
            .join(Course, Leaf.course_id == Course.id)
            .where(
                Course.account_id == runtime.account_id,
                Leaf.kind.in_(("video", "unknown")),
                Leaf.duration.is_not(None),
            )
            .order_by(Leaf.last_progress_at.is_(None).desc(), Leaf.last_progress_at.asc())
            .limit(PROGRESS_CHECK_LIMIT)
        )
        if course_id is not None:
            q = q.where(Leaf.course_id == course_id)
        rows = s.execute(q).all()
        snapshots = [
            {
                "leaf_db_id": leaf.id,
                "leaf_id": leaf.leaf_id,
                "status": leaf.status,
                "progress": leaf.progress or 0.0,
                "watched_seconds": leaf.watched_seconds or 0.0,
                "platform_course_id": leaf.platform_course_id,
                "platform_user_id": leaf.platform_user_id,
                "classroom_id": course.classroom_id,
            }
            for leaf, course in rows
            if leaf.status in ("watching", "done", "failed") or (leaf.progress or 0) > 0 or (leaf.watched_seconds or 0) > 0
        ]

    user_default = client.session.user_id
    touched_courses: set[int] = set()

    for snap in snapshots:
        user_id = snap["platform_user_id"] or user_default
        platform_cid = snap["platform_course_id"] or snap["classroom_id"]
        if not user_id:
            continue
        try:
            await asyncio.sleep(0.3)
            prog = await client.get_video_progress(platform_cid, user_id, snap["classroom_id"], snap["leaf_id"])
        except SessionExpired:
            raise
        except ApiError:
            result["errors"] += 1
            continue
        result["checked"] += 1
        if not prog:
            continue
        with db_session() as s:
            leaf = s.get(Leaf, snap["leaf_db_id"])
            if leaf is None:
                continue
            changed = False
            if prog.get("video_length") and not leaf.duration:
                leaf.duration = float(prog["video_length"])
                changed = True
            if prog["completed"]:
                if leaf.status != "done":
                    result["completed"] += 1
                leaf.status = "done"
                leaf.progress = 1.0
                if leaf.duration:
                    leaf.watched_seconds = leaf.duration
                changed = True
            elif abs((leaf.progress or 0) - prog["rate"]) > 0.005:
                leaf.progress = prog["rate"]
                if prog.get("watch_length"):
                    leaf.watched_seconds = round(float(prog["watch_length"]))
                elif leaf.duration:
                    leaf.watched_seconds = round(leaf.duration * prog["rate"])
                changed = True
            if changed:
                result["updated"] += 1
                touched_courses.add(leaf.course_id)

    for cid in touched_courses:
        refresh_course_stats(cid)

    log_event(
        "success",
        "sync",
        f"进度刷新完成：补全时长 {result['enriched']} 个，核对 {result['checked']} 个，新增完成 {result['completed']} 个",
    )
    return result


async def _enrich_durations(runtime, course_id: int | None = None, budget_sec: float = 180.0) -> int:
    """多轮补全未知时长，直到补完或用完时间预算（后台任务，不阻塞看板）。

    已尝试过但仍拿不到时长的叶子会被排除，避免反复请求同一批数据。
    """
    total = 0
    attempted: set[int] = set()
    started = time_module.monotonic()
    while time_module.monotonic() - started < budget_sec:
        enriched, processed = await _enrich_round(runtime, course_id, exclude=attempted)
        total += enriched
        if processed == 0 or len(attempted) >= ENRICH_DURATION_LIMIT * 12:
            break
    return total


async def _enrich_round(runtime, course_id: int | None = None, exclude: set[int] | None = None) -> tuple[int, int]:
    """单轮补全：返回 (成功补全时长数, 处理的叶子数)。"""
    client = runtime.client
    if not client.session.uv_id:
        return 0, 0

    with db_session() as s:
        q = (
            select(Leaf, Course)
            .join(Course, Leaf.course_id == Course.id)
            .where(
                Course.account_id == runtime.account_id,
                Leaf.kind == "video",
                Leaf.duration.is_(None),
                Leaf.status.in_(("pending", "failed")),
            )
            .limit(ENRICH_DURATION_LIMIT)
        )
        if course_id is not None:
            q = q.where(Leaf.course_id == course_id)
        if exclude:
            q = q.where(Leaf.id.not_in(exclude))
        rows = s.execute(q).all()
        targets = [
            {"id": leaf.id, "leaf_id": leaf.leaf_id, "course_db_id": leaf.course_id, "classroom_id": course.classroom_id}
            for leaf, course in rows
        ]

    enriched = 0
    touched: set[int] = set()
    for t in targets:
        if exclude is not None:
            exclude.add(t["id"])
        try:
            await asyncio.sleep(0.3)
            info = await client.get_leaf_info(t["classroom_id"], t["leaf_id"])
        except SessionExpired:
            raise
        except ApiError:
            continue
        if not info:
            continue
        fields = extract_leaf_fields(info)
        if not fields.get("is_video"):
            with db_session() as s:
                leaf = s.get(Leaf, t["id"])
                if leaf is not None:
                    leaf.kind = "other"
                    leaf.status = "skipped"
                    leaf.error_msg = "非视频内容"
                    touched.add(leaf.course_id)
            continue
        with db_session() as s:
            leaf = s.get(Leaf, t["id"])
            if leaf is None:
                continue
            if fields.get("duration"):
                leaf.duration = fields["duration"]
                enriched += 1
            if fields.get("ccid"):
                leaf.ccid = fields["ccid"]
            if fields.get("course_id"):
                leaf.platform_course_id = fields["course_id"]
            if fields.get("user_id"):
                leaf.platform_user_id = fields["user_id"]
            touched.add(leaf.course_id)

    for cid in touched:
        refresh_course_stats(cid)
    return enriched, len(targets)
