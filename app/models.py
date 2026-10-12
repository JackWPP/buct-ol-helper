"""数据库模型。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def now() -> datetime:
    return datetime.now()


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), default="我的账号")
    base_url: Mapped[str] = mapped_column(String(256), default="")
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    university_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    uv_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 会话 Cookie（JSON 字符串，含 sessionid / csrftoken 等）
    session_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="logged_out")  # logged_out / active / expired
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class Course(Base):
    __tablename__ = "courses"
    __table_args__ = (UniqueConstraint("account_id", "classroom_id", name="uq_course"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    classroom_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(256), default="")
    teacher: Mapped[str | None] = mapped_column(String(128), nullable=True)
    cover_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    sku_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    course_sign: Mapped[str | None] = mapped_column(String(64), nullable=True)  # 平台页面 URL 需要
    is_network: Mapped[bool] = mapped_column(Boolean, default=False)  # 是否网络课程
    term: Mapped[str | None] = mapped_column(String(64), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)  # 是否参与自动刷课
    status: Mapped[str] = mapped_column(String(32), default="idle")  # idle/watching/paused/done
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    total_videos: Mapped[int] = mapped_column(Integer, default=0)
    done_videos: Mapped[int] = mapped_column(Integer, default=0)
    total_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    watched_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    leaves: Mapped[list["Leaf"]] = relationship(back_populates="course", cascade="all, delete-orphan")


class Leaf(Base):
    __tablename__ = "leaves"
    __table_args__ = (UniqueConstraint("course_id", "leaf_id", name="uq_leaf"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), index=True)
    leaf_id: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(512), default="")
    # video / other
    kind: Mapped[str] = mapped_column(String(32), default="unknown")
    kind_hint: Mapped[str | None] = mapped_column(String(32), nullable=True)  # 平台原始类型提示（作业/考试/讨论）
    raw_leaf_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    # pending / watching / done / failed / skipped
    status: Mapped[str] = mapped_column(String(32), default="pending")
    progress: Mapped[float] = mapped_column(Float, default=0.0)  # 0..1
    duration: Mapped[float | None] = mapped_column(Float, nullable=True)  # 秒
    watched_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    ccid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sku_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    platform_course_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    platform_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_progress_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    course: Mapped[Course] = relationship(back_populates="leaves")


class EventLog(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    level: Mapped[str] = mapped_column(String(16), default="info")  # info/warning/error/success
    category: Mapped[str] = mapped_column(String(32), default="system")
    message: Mapped[str] = mapped_column(Text, default="")
    data_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
