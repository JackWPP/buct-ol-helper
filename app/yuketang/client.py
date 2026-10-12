"""雨课堂 API 客户端。

要点：
- 所有请求显式携带 Cookie 头（不依赖 httpx cookie jar），保证跨子域会话可控；
- 统一识别会话过期并抛出 SessionExpired；
- 对平台返回做尽量宽容的解析（字段名兼容多种形态）。
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx

from . import constants as C


class SessionExpired(Exception):
    """登录态失效，需要重新扫码。"""


class ApiError(Exception):
    def __init__(self, message: str, code: Any = None, payload: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.payload = payload


def _find_key(obj: Any, names: tuple[str, ...]) -> Any:
    """递归查找 JSON 中第一个匹配的键。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in names and v not in (None, ""):
                return v
        for v in obj.values():
            found = _find_key(v, names)
            if found not in (None, ""):
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_key(item, names)
            if found not in (None, ""):
                return found
    return None


def normalize_duration_seconds(v: Any) -> float | None:
    """把可能是毫秒/秒的时长统一成秒。"""
    try:
        val = float(v)
    except (TypeError, ValueError):
        return None
    if val <= 0:
        return None
    if val >= 100000:  # 大概率是毫秒
        return round(val / 1000.0, 1)
    if val > 14400 and val % 1000 == 0:  # 4 小时以上且整千，按毫秒处理
        return round(val / 1000.0, 1)
    return round(val, 1)


class AccountSession:
    """账号会话（可序列化）。"""

    def __init__(
        self,
        base_url: str,
        cookies: dict[str, str] | None = None,
        user_id: str | None = None,
        display_name: str | None = None,
        university_id: str | None = None,
        uv_id: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.cookies: dict[str, str] = dict(cookies or {})
        self.user_id = user_id
        self.display_name = display_name
        self.university_id = university_id
        self.uv_id = uv_id

    def cookie_header(self) -> str:
        base = {
            "platform_id": "3",
            "xtbz": "cloud",
            "platform_type": "1",
        }
        merged = {**base, **{k: v for k, v in self.cookies.items() if v}}
        if self.university_id:
            merged.setdefault("university_id", str(self.university_id))
        return "; ".join(f"{k}={v}" for k, v in merged.items())

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "cookies": self.cookies,
            "user_id": self.user_id,
            "display_name": self.display_name,
            "university_id": self.university_id,
            "uv_id": self.uv_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AccountSession":
        return cls(
            base_url=d.get("base_url", ""),
            cookies=d.get("cookies") or {},
            user_id=d.get("user_id"),
            display_name=d.get("display_name"),
            university_id=d.get("university_id"),
            uv_id=d.get("uv_id"),
        )


class YktClient:
    def __init__(self, session: AccountSession, settings) -> None:
        self.session = session
        self.settings = settings
        self._http = httpx.AsyncClient(timeout=settings.get_float("engine.request_timeout", 15.0))

    async def close(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------
    # 基础请求
    # ------------------------------------------------------------------

    def _headers(self, referer: str | None = None, extra: dict[str, str] | None = None) -> dict[str, str]:
        h: dict[str, str] = {
            "User-Agent": C.UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "x-client": "web",
            "platform-type": "1",
            "xtbz": "cloud",
            "Origin": self.session.base_url,
            "Referer": referer or f"{self.session.base_url}/pro/courselist",
        }
        if self.session.university_id:
            h["university-id"] = str(self.session.university_id)
        csrf = self.session.cookies.get("csrftoken")
        if csrf:
            h["X-CSRFToken"] = csrf
        h["Cookie"] = self.session.cookie_header()
        if extra:
            h.update(extra)
        return h

    def _video_headers(self, classroom_id: str) -> dict[str, str]:
        """视频学习相关接口的上下文头。

        平台的 mooc-api / video-log 体系需要 xtbz=ykt 且显式携带 classroom-id，
        否则会返回 80005（you did not buy this product）或 40000（CLASSROOM ID IS REQUIRED）。
        """
        return {"xtbz": "ykt", "classroom-id": str(classroom_id), "platform-id": "3"}

    def _check_expired(self, status_code: int, text: str) -> None:
        if status_code == 401:
            raise SessionExpired("平台返回 401")
        for marker in C.SESSION_EXPIRED_MARKERS:
            if marker in text:
                raise SessionExpired(marker)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        referer: str | None = None,
        extra_headers: dict[str, str] | None = None,
        retries: int = 2,
        return_text: bool = False,
    ) -> Any:
        url = path if path.startswith("http") else f"{self.session.base_url}{path}"
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = await self._http.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=self._headers(referer, extra_headers),
                )
                text = resp.text
                self._check_expired(resp.status_code, text)
                if return_text:
                    return text
                try:
                    return resp.json()
                except Exception:
                    return {"_raw": text[:2000]}
            except SessionExpired:
                raise
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last_exc = e
                if attempt < retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise ApiError(f"网络请求失败: {e}") from e
        raise ApiError(f"网络请求失败: {last_exc}")

    # ------------------------------------------------------------------
    # 会话 / 用户
    # ------------------------------------------------------------------

    async def get_user_info_page(self) -> str:
        """访问 user_info 页面（用于拿 csrftoken，也用于测试连通性）。"""
        return await self._request("GET", C.PATH_USER_INFO_PAGE, return_text=True)

    async def refresh_csrf(self) -> str | None:
        """访问 user_info 页面并从 Set-Cookie 中刷新 csrftoken。"""
        url = f"{self.session.base_url}{C.PATH_USER_INFO_PAGE}"
        resp = await self._http.get(url, headers=self._headers())
        for sc in resp.headers.get_list("set-cookie"):
            m = re.match(r"csrftoken=([^;]+)", sc)
            if m:
                self.session.cookies["csrftoken"] = m.group(1)
                return m.group(1)
        return self.session.cookies.get("csrftoken")

    async def get_basic_info(self) -> dict:
        return await self._request("GET", C.PATH_BASIC_INFO)

    async def get_userinfo(self) -> str:
        return await self._request("GET", C.PATH_USERINFO, return_text=True)

    # ------------------------------------------------------------------
    # 课程 / 章节 / 叶子
    # ------------------------------------------------------------------

    async def get_courses(self) -> list[dict]:
        payload = await self._request("GET", C.PATH_COURSES, params={"identity": "2"})
        if not isinstance(payload, dict):
            raise ApiError(f"课程列表返回异常: {str(payload)[:200]}")
        errmsg = payload.get("errmsg")
        if errmsg and errmsg != "Success":
            raise ApiError(f"获取课程列表失败: {errmsg}", code=payload.get("errcode"), payload=payload)
        data = payload.get("data") or {}
        items = data.get("list") or []
        courses = []
        for item in items:
            course_meta = item.get("course") or {}
            teacher = item.get("teacher") or {}
            courses.append(
                {
                    "classroom_id": str(item.get("classroom_id") or item.get("id") or ""),
                    "name": course_meta.get("name") or item.get("name") or item.get("title") or "未命名课程",
                    "teacher": teacher.get("name") or item.get("teacher_name") or "",
                    "cover": course_meta.get("cover") or course_meta.get("cover_url") or item.get("cover") or "",
                    "term": str(item.get("term") or item.get("term_id") or ""),
                    "raw": item,
                }
            )
        return [c for c in courses if c["classroom_id"]]

    async def get_classroom(self, classroom_id: str) -> dict:
        """课程详情：含 free_sku_id / uv_id（学校标识）/ course_id / course_sign。"""
        try:
            payload = await self._request("GET", C.PATH_CLASSROOM.format(cid=classroom_id), params={"role": "5"})
        except ApiError:
            return {}
        if not isinstance(payload, dict):
            return {}
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    async def get_sku_id(self, classroom_id: str) -> str | None:
        data = await self.get_classroom(classroom_id)
        sku = data.get("free_sku_id") or data.get("sku_id")
        return str(sku) if sku else None

    async def get_leaf_level_infos(self, sku_id: str) -> list[dict]:
        payload = await self._request("GET", C.PATH_SCORE_DETAIL.format(sku_id=sku_id))
        if not isinstance(payload, dict):
            return []
        data = payload.get("data") or {}
        infos = data.get("leaf_level_infos") or []
        return [x for x in infos if isinstance(x, dict)]

    async def get_leaf_info(self, classroom_id: str, leaf_id: str) -> dict | None:
        """叶子详情（视频时长 / 媒体 id / 平台用户 id 等）。

        注意：必须带 xtbz=ykt + classroom-id 头，否则返回 80005 / 40000。
        """
        payload = await self._request(
            "GET",
            C.PATH_LEAF_INFO.format(cid=classroom_id, leaf_id=leaf_id),
            extra_headers=self._video_headers(classroom_id),
        )
        if not isinstance(payload, dict):
            return None
        if payload.get("success") is False or payload.get("error_code"):
            return None
        data = payload.get("data")
        return data if isinstance(data, dict) else None

    # ------------------------------------------------------------------
    # 视频进度 / 心跳
    # ------------------------------------------------------------------

    async def get_video_progress(
        self,
        course_id: str,
        user_id: str,
        classroom_id: str,
        video_id: str,
    ) -> dict | None:
        uv_id = self.session.uv_id or self.session.university_id or ""
        params = {
            "cid": course_id,
            "user_id": user_id,
            "classroom_id": classroom_id,
            "video_type": "video",
            "vtype": "rate",
            "video_id": video_id,
            "snapshot": "1",
            "term": "latest",
            "uv_id": str(uv_id),
        }
        referer = f"{self.session.base_url}/pro/lms/{''}/{classroom_id}/video/{video_id}"
        payload = await self._request(
            "GET",
            C.PATH_VIDEO_PROGRESS,
            params=params,
            referer=referer,
            extra_headers=self._video_headers(classroom_id),
        )
        return parse_video_progress(payload)

    async def post_heartbeat(self, events: list[dict], sku_id: str, classroom_id: str, leaf_id: str) -> dict | None:
        referer = f"{self.session.base_url}/pro/lms/{sku_id}/{classroom_id}/video/{leaf_id}"
        headers = {**self._video_headers(classroom_id), "Content-Type": "application/json"}
        payload = await self._request(
            "POST",
            C.PATH_HEARTBEAT,
            json_body={"heart_data": events},
            referer=referer,
            extra_headers=headers,
        )
        if isinstance(payload, dict):
            return payload
        return {"_raw": payload}

    # ------------------------------------------------------------------
    # 诊断探针
    # ------------------------------------------------------------------

    async def probe(self) -> dict:
        """依次探测关键接口并返回脱敏后的原始响应片段，便于排查平台改版。"""

        async def wrap(name: str, coro):
            try:
                result = await coro
                if isinstance(result, str):
                    sample = result[:800]
                else:
                    sample = json.dumps(result, ensure_ascii=False)[:800]
                return {"ok": True, "sample": sample}
            except SessionExpired as e:
                return {"ok": False, "error": f"会话失效: {e}"}
            except Exception as e:
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}

        out: dict[str, Any] = {}
        out["basic_info"] = await wrap("basic_info", self.get_basic_info())
        out["userinfo"] = await wrap("userinfo", self.get_userinfo())
        courses = await self.get_courses()
        out["courses_count"] = len(courses)
        out["courses_sample"] = courses[:2]
        if courses:
            cid = courses[0]["classroom_id"]
            sku = await self.get_sku_id(cid)
            out["sku_id"] = sku
            if sku:
                infos = await self.get_leaf_level_infos(sku)
                out["leaf_count"] = len(infos)
                out["leaf_sample"] = infos[:3]
                if infos:
                    leaf_id = str(infos[0].get("id"))
                    info = await self.get_leaf_info(cid, leaf_id)
                    out["leaf_info_sample"] = json.dumps(info, ensure_ascii=False)[:800] if info else None
        return out


# ----------------------------------------------------------------------
# 解析工具
# ----------------------------------------------------------------------

def parse_video_progress(payload: Any) -> dict | None:
    """解析进度接口返回。

    平台返回形如：
      {"data": {"<video_id>": {"completed": 0, "rate": 0.53, "last_point": 341.5,
                               "watch_length": 342.5, "video_length": 643.0}}}
    """
    if payload is None:
        return None
    raw = payload
    found: dict[str, float] = {}

    def scan(obj: Any) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in ("completed", "rate", "last_point", "watch_length", "video_length") and k not in found:
                    try:
                        found[k] = float(v)
                    except (TypeError, ValueError):
                        pass
                else:
                    scan(v)
        elif isinstance(obj, list):
            for item in obj:
                scan(item)

    scan(payload)
    if not found and isinstance(payload, str):
        for key in ("completed", "rate", "last_point", "watch_length", "video_length"):
            m = re.search(rf'"{key}"\s*:\s*([0-9.]+)', payload)
            if m:
                found[key] = float(m.group(1))
    if not found:
        return None

    completed = int(found.get("completed", 0)) if "completed" in found else None
    rate = found.get("rate")
    return {
        "completed": bool(completed) if completed is not None else (rate is not None and rate >= 0.999),
        "rate": rate if rate is not None else (1.0 if completed else 0.0),
        "last_point": found.get("last_point"),
        "watch_length": found.get("watch_length"),
        "video_length": found.get("video_length"),
        "raw": raw,
    }


def extract_leaf_fields(info: dict) -> dict:
    """从 leaf_info 中提取心跳所需字段。

    注意：非视频叶子（推荐/讨论/作业/考试）的 content_info.media 是空壳
    （形如 {"duration": 0, "ccid": ""}），所以必须按"值是否为空"判断，不能只看键。
    """
    content = info.get("content_info") or {}
    media = content.get("media") if isinstance(content.get("media"), dict) else {}
    leaf_type_raw = content.get("leaf_type", info.get("leaf_type"))
    leaf_type = str(leaf_type_raw) if leaf_type_raw is not None else None

    duration = None
    for source in (media, content, info):
        if not isinstance(source, dict):
            continue
        for key in ("duration", "video_duration", "media_duration", "total_length", "length"):
            if key in source:
                duration = normalize_duration_seconds(source.get(key))
                if duration:
                    break
        if duration:
            break

    ccid = None
    if isinstance(media, dict):
        for key in ("ccid", "cc", "ccurl", "media_id", "mediaId", "video_id"):
            if media.get(key):
                ccid = str(media[key])
                break

    video_obj = content.get("video") or info.get("video")
    has_video_obj = isinstance(video_obj, dict) and any(bool(v) for v in video_obj.values())

    non_video_types = {str(t) for t in C.NON_VIDEO_LEAF_TYPES}
    video_types = {str(t) for t in C.VIDEO_LEAF_TYPES}

    if leaf_type in non_video_types:
        is_video = False
    elif leaf_type in video_types:
        is_video = bool(ccid) or has_video_obj or bool(duration)
    else:
        # 类型不明确时，必须有真实媒体证据
        is_video = bool(ccid) or has_video_obj
    if is_video and not ccid and not has_video_obj and not duration:
        is_video = False

    return {
        "leaf_type": leaf_type,
        "is_video": is_video,
        "duration": duration,
        "ccid": ccid,
        "sku_id": str(info.get("sku_id")) if info.get("sku_id") else None,
        "user_id": str(info.get("user_id")) if info.get("user_id") else None,
        "course_id": str(info.get("course_id")) if info.get("course_id") else None,
    }


def extract_identity(userinfo_text: str, basic_info: dict | None = None) -> dict:
    """从用户信息中提取 user_id / university_id / uv_id。"""
    out: dict[str, Any] = {}
    if userinfo_text:
        m = re.search(r'"user_id"\s*:\s*"?([0-9]+)"?', userinfo_text)
        if not m:
            m = re.search(r"user_id[\"']?\s*[:=]\s*[\"']?([0-9]+)", userinfo_text)
        if m:
            out["user_id"] = m.group(1)
        m = re.search(r'"university_id"\s*:\s*"?([0-9]+)"?', userinfo_text)
        if m:
            out["university_id"] = m.group(1)
        m = re.search(r'"uv_id"\s*:\s*"?([0-9]+)"?', userinfo_text)
        if m:
            out["uv_id"] = m.group(1)
    if basic_info:
        uid = _find_key(basic_info, ("user_id", "userId"))
        if uid and "user_id" not in out:
            out["user_id"] = str(uid)
        name = _find_key(basic_info, ("nickname", "name", "display_name"))
        if name:
            out["display_name"] = str(name)
        uv = _find_key(basic_info, ("university_id", "school_id"))
        if uv and "university_id" not in out:
            out["university_id"] = str(uv)
        uvv = _find_key(basic_info, ("uv_id",))
        if uvv and "uv_id" not in out:
            out["uv_id"] = str(uvv)
    return out
