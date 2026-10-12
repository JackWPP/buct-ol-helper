"""平台接口路径与常量（集中管理，平台更新时只改这里）。"""
from __future__ import annotations

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

# ---- 平台接口 ----
PATH_USER_INFO_PAGE = "/v/course_meta/user_info"          # 用于获取 csrftoken
PATH_BASIC_INFO = "/api/v3/user/basic-info"               # 会话校验
PATH_USERINFO = "/v2/api/web/userinfo"                    # 用户信息（含 user_id）
PATH_WECHAT_AUTH_PARAM = "/api/v3/user/login/wechat-auth-param"  # 微信扫码登录参数
PATH_COURSES = "/v2/api/web/courses/list"                 # 课程列表 ?identity=2
PATH_CLASSROOM = "/v2/api/web/classrooms/{cid}"           # 课程详情 ?role=5 → free_sku_id
PATH_SCORE_DETAIL = "/c27/online_courseware/schedule/score_detail/single/{sku_id}/0/"  # 章节/叶子列表
PATH_LEAF_INFO = "/mooc-api/v1/lms/learn/leaf_info/{cid}/{leaf_id}/"  # 叶子详情
PATH_VIDEO_PROGRESS = "/video-log/get_video_watch_progress/"          # 视频进度查询
PATH_HEARTBEAT = "/video-log/heartbeat/"                              # 心跳上报
PATH_COURSEWARE_LEARN = "/v2/api/web/logs/learn/{cid}"    # 备用：课程章节日志
PATH_PUB_NEW = "/mooc-api/v1/lms/learn/course/pub_new_pro"  # 备用：章节完成状态

# 微信开放平台（扫码登录）
WX_QRCONNECT = "https://open.weixin.qq.com/connect/qrconnect"
WX_QRCODE = "https://open.weixin.qq.com/connect/qrcode/{uuid}"
WX_POLL = "https://lp.open.weixin.qq.com/connect/l/qrconnect"

# 视频心跳：真实前端行为参数
HEARTBEAT_EVENT = "heartbeat"          # 播放中周期性事件类型
HEARTBEAT_DEFAULT_I = 5                # 节拍标识（秒）
HEARTBEAT_PARAMS_T = "video"           # 内容类型
HEARTBEAT_P = "web"                    # 端标识
MAX_EVENTS_PER_FLUSH = 50              # 单次 POST 最多事件数（前端上限）

# et 字段的事件名白名单（实测平台校验结果，非法值会返回 "Partial data anomaly"）：
#   合法: loadstart / loadeddata / play / playing / pause / waiting / stalled / error /
#         ratechange / seeking / heartbeat / videoend
#   非法: ended / canplay / canplaythrough / timeupdate / seeked / loadedmetadata 等
EVENTS_START = ("loadstart", "loadeddata", "play", "playing")
EVENTS_END = ("pause", "videoend")
EVENTS_RESUME = ("seeking", "play", "playing")

# 视频叶子判定
VIDEO_LEAF_TYPES = {0, "0", "video"}
# 明确的非视频叶子类型（推荐阅读 / 讨论 / 考试 / 作业）
NON_VIDEO_LEAF_TYPES = {3, 4, 5, 6, "3", "4", "5", "6"}

# 明显非视频的标题关键词（仅在无法获取 leaf_info 元信息时兜底使用）
NON_VIDEO_TITLE_KEYWORDS = ["作业", "考试", "测验", "主观题", "公告", "问卷", "投票", "讨论"]

# leaf_type → 人工处理类型提示（这些内容不自动刷，需要去平台手动完成）
LEAF_TYPE_HINT = {
    "3": "推荐阅读",
    "4": "讨论",
    "5": "考试",
    "6": "作业",
    "8": "直播/回放",
}

# 判定"网络课程"的关键词（这些才需要刷视频；教师自建课一般没有网课视频）
NETWORK_COURSE_KEYWORDS = ["网络课程", "慕课", "MOOC"]

# 会话过期特征
SESSION_EXPIRED_MARKERS = [
    "Cookie has no sessionid",
    "UNAUTHENTICATED",
    "User is not login",
    "未登录",
    "登录已过期",
]
