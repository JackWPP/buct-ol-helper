"""数据库初始化与常用操作。"""
from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Iterator

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from .config import data_dir
from .models import Account, Base, EventLog

_engine = None
_SessionLocal: sessionmaker | None = None

# 轻量迁移：老库缺列时补上（SQLite 的 create_all 不会改已存在的表）
MIGRATIONS: list[tuple[str, str]] = [
    ("courses", "course_sign VARCHAR(64)"),
    ("courses", "is_network BOOLEAN DEFAULT 0"),
    ("leaves", "kind_hint VARCHAR(32)"),
]


def init_db() -> None:
    global _engine, _SessionLocal
    if _engine is not None:
        return
    db_path = data_dir() / "app.db"
    _engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
        future=True,
    )
    # WAL 模式：读写并发不互相阻塞（否则后台刷新任务写库时，看板操作会卡住）
    with _engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
        conn.execute(text("PRAGMA synchronous=NORMAL"))
        conn.execute(text("PRAGMA busy_timeout=30000"))
        conn.commit()
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    Base.metadata.create_all(_engine)
    _ensure_columns()


def _ensure_columns() -> None:
    assert _engine is not None
    with _engine.connect() as conn:
        for table, column_def in MIGRATIONS:
            try:
                cols = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
            except Exception:
                continue
            col_name = column_def.split()[0]
            if col_name not in cols:
                try:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column_def}"))
                    conn.commit()
                except Exception:
                    pass


@contextmanager
def db_session() -> Iterator[Session]:
    assert _SessionLocal is not None, "init_db() 未调用"
    s = _SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def log_event(level: str, category: str, message: str, data: Any = None, session: Session | None = None) -> None:
    """写一条看板事件日志。"""
    payload = None
    if data is not None:
        try:
            payload = json.dumps(data, ensure_ascii=False, default=str)
        except Exception:
            payload = str(data)
    if session is not None:
        session.add(EventLog(level=level, category=category, message=message, data_json=payload))
        return
    with db_session() as s:
        s.add(EventLog(level=level, category=category, message=message, data_json=payload))


def recent_events(after_id: int = 0, limit: int = 200) -> list[dict]:
    with db_session() as s:
        q = select(EventLog).where(EventLog.id > after_id).order_by(EventLog.id.desc()).limit(limit)
        rows = list(s.scalars(q))
    return [
        {
            "id": r.id,
            "ts": r.ts.strftime("%m-%d %H:%M:%S"),
            "level": r.level,
            "category": r.category,
            "message": r.message,
        }
        for r in rows
    ]


def get_or_create_account(base_url: str, name: str = "我的账号") -> Account:
    with db_session() as s:
        acct = s.scalar(select(Account).order_by(Account.id).limit(1))
        if acct is None:
            acct = Account(base_url=base_url, name=name)
            s.add(acct)
            s.flush()
        elif base_url and acct.base_url != base_url:
            acct.base_url = base_url
        return acct
