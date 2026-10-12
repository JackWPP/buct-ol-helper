"""看板 API。"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select

from ..db import db_session, log_event, recent_events
from ..engine.manager import refresh_course_stats
from ..models import Account, Course, Leaf
from ..yuketang.client import SessionExpired

router = APIRouter()


def rt(request: Request):
    return request.app.state.runtime


# ======================================================================
# 登录相关
# ======================================================================


class CookiePayload(BaseModel):
    cookie: str


@router.post("/auth/qr")
async def create_qr(request: Request):
    runtime = rt(request)
    try:
        return await runtime.auth.create_qr()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"获取二维码失败：{e}")


@router.get("/auth/qr/{key}")
async def poll_qr(key: str, request: Request):
    runtime = rt(request)
    return await runtime.auth.poll(key)


@router.post("/auth/cookie")
async def import_cookie(payload: CookiePayload, request: Request):
    runtime = rt(request)
    try:
        user = await runtime.auth.import_cookie_string(payload.cookie)
        return {"ok": True, "user": user}
    except SessionExpired:
        raise HTTPException(status_code=401, detail="Cookie 无效或已过期")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"导入失败：{e}")


@router.get("/auth/status")
async def auth_status(request: Request):
    runtime = rt(request)
    acct = None
    if runtime.account_id:
        with db_session() as s:
            acct = s.get(Account, runtime.account_id)
    return {
        "logged_in": runtime.logged_in,
        "user": runtime.user_public(),
        "status": acct.status if acct else "logged_out",
        "last_check_at": acct.last_check_at.strftime("%m-%d %H:%M:%S") if acct and acct.last_check_at else None,
        "base_url": runtime.base_url,
    }


@router.post("/auth/check")
async def auth_check(request: Request):
    runtime = rt(request)
    result = await runtime.check_session()
    return {"ok": bool(result), "logged_in": runtime.logged_in}


@router.post("/auth/logout")
async def auth_logout(request: Request):
    runtime = rt(request)
    await runtime.logout()
    return {"ok": True}


# ======================================================================
# 课程与视频
# ======================================================================


def _course_public(course: Course, eta_speed: float, eta_parallel: int, base_url: str = "") -> dict:
    remaining = max(0.0, (course.total_seconds or 0) - (course.watched_seconds or 0))
    eta_seconds = remaining / eta_speed / max(1, eta_parallel)
    sign = course.course_sign or course.sku_id or ""
    platform_url = (
        f"{base_url}/pro/lms/{sign}/{course.classroom_id}/studycontent" if sign else f"{base_url}/pro/courselist"
    )
    return {
        "id": course.id,
        "classroom_id": course.classroom_id,
        "name": course.name,
        "teacher": course.teacher,
        "cover": course.cover_url,
        "is_network": bool(course.is_network),
        "platform_url": platform_url,
        "enabled": course.enabled,
        "status": course.status,
        "total_videos": course.total_videos,
        "done_videos": course.done_videos,
        "total_seconds": round(course.total_seconds or 0),
        "watched_seconds": round(course.watched_seconds or 0),
        "progress": round((course.watched_seconds or 0) / course.total_seconds, 4) if course.total_seconds else 0.0,
        "eta_seconds": round(eta_seconds),
        "last_sync_at": course.last_sync_at.strftime("%m-%d %H:%M") if course.last_sync_at else None,
    }


LEAF_SUBPATH = {"作业": "homework", "考试": "exam", "讨论": "forum", "直播/回放": "live"}


def _leaf_public(leaf: Leaf, course: Course, base_url: str = "") -> dict:
    sign = course.course_sign or course.sku_id or ""
    sub = LEAF_SUBPATH.get(leaf.kind_hint or "", "video" if leaf.kind == "video" else "studycontent")
    url = (
        f"{base_url}/pro/lms/{sign}/{course.classroom_id}/{sub}/{leaf.leaf_id}"
        if sign and sub != "studycontent"
        else (f"{base_url}/pro/lms/{sign}/{course.classroom_id}/studycontent" if sign else f"{base_url}/pro/courselist")
    )
    return {
        "id": leaf.id,
        "leaf_id": leaf.leaf_id,
        "title": leaf.title,
        "kind": leaf.kind,
        "kind_hint": leaf.kind_hint,
        "status": leaf.status,
        "progress": round(leaf.progress or 0, 4),
        "duration": round(leaf.duration) if leaf.duration else None,
        "watched_seconds": round(leaf.watched_seconds or 0),
        "error_msg": leaf.error_msg,
        "needs_manual": bool(leaf.kind_hint and leaf.kind == "other"),
        "platform_url": url,
    }


@router.get("/courses")
async def list_courses(request: Request):
    runtime = rt(request)
    speed = max(0.5, runtime.settings.get_float("engine.speed", 1.0))
    parallel = max(1, runtime.settings.get_int("engine.max_parallel_videos", 2))
    with db_session() as s:
        rows = s.scalars(select(Course).order_by(Course.order_index, Course.id)).all()
        courses = [_course_public(c, speed, parallel, runtime.base_url) for c in rows]
    return {"courses": courses}


@router.get("/courses/{course_id}/leaves")
async def course_leaves(course_id: int, request: Request):
    runtime = rt(request)
    with db_session() as s:
        course = s.get(Course, course_id)
        if course is None:
            raise HTTPException(status_code=404, detail="课程不存在")
        leaves = s.scalars(select(Leaf).where(Leaf.course_id == course_id).order_by(Leaf.order_index, Leaf.id)).all()
        sign = course.course_sign or course.sku_id or ""
        data = {
            "course": {
                "id": course.id,
                "name": course.name,
                "classroom_id": course.classroom_id,
                "teacher": course.teacher,
                "status": course.status,
                "enabled": course.enabled,
                "is_network": bool(course.is_network),
                "platform_url": (
                    f"{runtime.base_url}/pro/lms/{sign}/{course.classroom_id}/studycontent"
                    if sign
                    else f"{runtime.base_url}/pro/courselist"
                ),
            },
            "leaves": [_leaf_public(l, course, runtime.base_url) for l in leaves],
        }
    return data


@router.post("/courses/{course_id}/start")
async def course_start(course_id: int, request: Request):
    runtime = rt(request)
    with db_session() as s:
        course = s.get(Course, course_id)
        if course is None:
            raise HTTPException(status_code=404, detail="课程不存在")
        course.enabled = True
    await runtime.manager.resume_course(course_id)
    await runtime.manager.start([course_id])
    return {"ok": True}


@router.post("/courses/{course_id}/pause")
async def course_pause(course_id: int, request: Request):
    runtime = rt(request)
    await runtime.manager.pause_course(course_id)
    return {"ok": True}


class EnablePayload(BaseModel):
    enabled: bool


@router.post("/courses/{course_id}/enable")
async def course_enable(course_id: int, payload: EnablePayload, request: Request):
    runtime = rt(request)
    with db_session() as s:
        course = s.get(Course, course_id)
        if course is None:
            raise HTTPException(status_code=404, detail="课程不存在")
        course.enabled = payload.enabled
    if not payload.enabled:
        await runtime.manager.pause_course(course_id)
    return {"ok": True}


@router.post("/courses/{course_id}/refresh")
async def course_refresh(course_id: int, request: Request):
    runtime = rt(request)
    asyncio.create_task(runtime.run_progress_refresh(course_id))
    return {"ok": True}


@router.post("/courses/refresh-all")
async def courses_refresh_all(request: Request):
    runtime = rt(request)
    asyncio.create_task(runtime.run_progress_refresh())
    return {"ok": True}


@router.post("/courses/sync")
async def courses_sync(request: Request):
    runtime = rt(request)
    asyncio.create_task(runtime.run_sync())
    return {"ok": True, "job": runtime.jobs["sync"]}


@router.post("/courses/auto-select")
async def courses_auto_select(request: Request):
    """按规则勾选：只启用"网络课程"（且有视频）；其余课程不参与自动刷课。"""
    runtime = rt(request)
    with db_session() as s:
        rows = s.scalars(select(Course).where(Course.account_id == runtime.account_id)).all()
        picked = 0
        skipped = 0
        for row in rows:
            should = bool(row.is_network) and (row.total_videos or 0) > 0
            row.enabled = should
            if should:
                picked += 1
            else:
                skipped += 1
    log_event("info", "sync", f"手动自动勾选：启用 {picked} 门网络课程，排除 {skipped} 门")
    return {"ok": True, "enabled": picked, "excluded": skipped}


class LeafAction(BaseModel):
    action: str  # retry / skip / reset


@router.post("/leaves/{leaf_id}/action")
async def leaf_action(leaf_id: int, payload: LeafAction, request: Request):
    runtime = rt(request)
    with db_session() as s:
        leaf = s.get(Leaf, leaf_id)
        if leaf is None:
            raise HTTPException(status_code=404, detail="章节不存在")
        course_id = leaf.course_id
        course = s.get(Course, course_id)
        if payload.action == "retry":
            leaf.status = "pending"
            leaf.error_msg = None
        elif payload.action == "skip":
            leaf.status = "skipped"
            leaf.error_msg = "手动跳过"
        elif payload.action == "reset":
            leaf.status = "pending"
            leaf.progress = 0.0
            leaf.watched_seconds = 0.0
            leaf.error_msg = None
        else:
            raise HTTPException(status_code=400, detail="未知操作")
        enabled = course.enabled if course else False
    refresh_course_stats(course_id)
    if payload.action in ("retry", "reset") and enabled:
        await runtime.manager.resume_course(course_id)
    return {"ok": True}


# ======================================================================
# 引擎控制
# ======================================================================


@router.post("/engine/start")
async def engine_start(request: Request):
    runtime = rt(request)
    if not runtime.logged_in:
        raise HTTPException(status_code=400, detail="尚未登录，请先扫码")
    started = await runtime.manager.start()
    return {"ok": True, "started_courses": started}


@router.post("/engine/stop")
async def engine_stop(request: Request):
    runtime = rt(request)
    await runtime.manager.stop()
    return {"ok": True}


@router.get("/engine/status")
async def engine_status(request: Request):
    runtime = rt(request)
    return {"engine": runtime.manager.status(), "jobs": runtime.jobs}


# ======================================================================
# 系统 / 看板
# ======================================================================


@router.get("/system/overview")
async def overview(request: Request):
    runtime = rt(request)
    speed = max(0.5, runtime.settings.get_float("engine.speed", 1.0))
    parallel = max(1, runtime.settings.get_int("engine.max_parallel_videos", 2))

    with db_session() as s:
        courses = s.scalars(select(Course).order_by(Course.order_index, Course.id)).all()
        enabled = [c for c in courses if c.enabled]
        total_videos = sum(c.total_videos or 0 for c in courses)
        done_videos = sum(c.done_videos or 0 for c in courses)
        total_seconds = sum(c.total_seconds or 0 for c in courses)
        watched_seconds = sum(c.watched_seconds or 0 for c in courses)
        watching = [c.name for c in courses if c.status == "watching"]
        course_list = [_course_public(c, speed, parallel, runtime.base_url) for c in courses]

    remaining = max(0.0, total_seconds - watched_seconds)
    eta_parallel = max(1, min(parallel, len(enabled) if enabled else 1))
    eta_seconds = remaining / speed / eta_parallel

    return {
        "account": {
            "logged_in": runtime.logged_in,
            "user": runtime.user_public(),
            "base_url": runtime.base_url,
        },
        "engine": {
            **runtime.manager.status(),
            "speed": speed,
            "max_parallel": parallel,
            "dry_run": runtime.settings.get_bool("engine.dry_run", False),
        },
        "jobs": runtime.jobs,
        "stats": {
            "courses": len(courses),
            "enabled_courses": len(enabled),
            "videos": total_videos,
            "done_videos": done_videos,
            "total_seconds": round(total_seconds),
            "watched_seconds": round(watched_seconds),
            "remaining_seconds": round(remaining / speed),
            "eta_seconds": round(eta_seconds),
            "watching": watching,
        },
        "courses": course_list,
        "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


@router.get("/system/logs")
async def system_logs(after_id: int = 0, limit: int = 120):
    return {"logs": recent_events(after_id=after_id, limit=min(limit, 500))}


@router.get("/system/live")
async def system_live(request: Request):
    """实时状态：秒级轮询用（只读内存，不查库）。"""
    runtime = rt(request)
    return {
        "engine": runtime.manager.status(),
        "active": runtime.manager.live_snapshot(),
        "server_time": time.time(),
    }


@router.post("/system/report")
async def system_report(request: Request):
    """立即发送一次进度汇报。"""
    runtime = rt(request)
    result = await runtime.send_progress_report(force=True)
    return {"ok": True, **result}


@router.get("/system/settings")
async def get_settings(request: Request):
    runtime = rt(request)
    snap = runtime.settings.snapshot()
    if snap.get("notify.email.password"):
        snap["notify.email.password"] = "******"
    if snap.get("notify.feishu.secret"):
        snap["notify.feishu.secret"] = "******"
    return snap


SETTING_TYPES: dict[str, type] = {
    "engine.speed": float,
    "engine.max_parallel_videos": int,
    "engine.heartbeat_tick": float,
    "engine.flush_interval": float,
    "engine.progress_check_interval": float,
    "engine.retry": int,
    "engine.dry_run": bool,
    "engine.auto_start": bool,
    "engine.auto_select_network_courses": bool,
    "engine.auto_refresh_minutes": int,
    "engine.report_interval_minutes": int,
    "notify.enabled": bool,
    "notify.feishu.webhook": str,
    "notify.feishu.secret": str,
    "notify.email.smtp_host": str,
    "notify.email.smtp_port": int,
    "notify.email.use_ssl": bool,
    "notify.email.username": str,
    "notify.email.password": str,
    "notify.email.sender": str,
    "notify.events.login": bool,
    "notify.events.login_expired": bool,
    "notify.events.hourly": bool,
    "notify.events.course_completed": bool,
    "notify.events.all_completed": bool,
    "notify.events.video_completed": bool,
    "notify.events.anomaly": bool,
    "notify.events.errors": bool,
}


class SettingsPayload(BaseModel):
    values: dict[str, Any]


@router.post("/system/settings")
async def update_settings(payload: SettingsPayload, request: Request):
    runtime = rt(request)
    applied = {}
    for key, value in payload.values.items():
        if key not in SETTING_TYPES:
            continue
        if isinstance(value, str) and value == "******":
            continue  # 未修改的密码占位
        typ = SETTING_TYPES[key]
        try:
            if typ is bool:
                value = bool(value)
            elif typ is int:
                value = int(value)
            elif typ is float:
                value = float(value)
            else:
                value = str(value)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"参数 {key} 类型错误")
        runtime.settings.set(key, value)
        applied[key] = value

    if "notify.email.to" in payload.values:
        to = payload.values["notify.email.to"]
        if isinstance(to, str):
            to = [x.strip() for x in to.split(",") if x.strip()]
        runtime.settings.set("notify.email.to", to)
        applied["notify.email.to"] = to

    # 参数变化后唤醒调度（并发数可能上调）
    runtime.manager.poke()
    log_event("info", "system", f"参数已更新：{', '.join(applied.keys()) or '无变化'}")
    return {"ok": True, "applied": applied}


@router.post("/system/notify/test")
async def notify_test(request: Request):
    runtime = rt(request)
    results = await runtime.notifier.send_test()
    return {"ok": True, "results": results}


@router.get("/system/probe")
async def probe(request: Request):
    runtime = rt(request)
    if not runtime.logged_in:
        raise HTTPException(status_code=400, detail="尚未登录，请先扫码")
    try:
        return await runtime.client.probe()
    except SessionExpired:
        await runtime._on_session_expired()  # noqa: SLF001
        raise HTTPException(status_code=401, detail="登录已过期")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"探针执行失败：{e}")
