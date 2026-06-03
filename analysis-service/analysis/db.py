"""
DB 연결 — 운영 DB (또는 read replica) 직접 접근용.

설계:
  - SQLAlchemy 2.0 + pymysql
  - read-only 패턴 (분석 서비스는 SELECT 만)
  - 시뮬: 같은 DB / 운영: read replica endpoint
  - 환경변수 DB_URL 만 변경하면 코드 변화 X

사용:
  from analysis.db import get_session

  with get_session() as session:
      result = session.execute(text("SELECT * FROM v_active_employee"))
      rows = result.mappings().all()
"""
import os
import logging
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


logger = logging.getLogger("analysis.db")


# ─── 싱글톤 엔진 ───
_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None


def get_engine() -> Engine:
    """싱글톤 엔진 반환. 첫 호출 시 .env 값으로 초기화."""
    global _engine, _SessionLocal
    if _engine is None:
        url = os.getenv("DB_URL")
        if not url:
            raise RuntimeError("DB_URL 환경변수 미설정")

        pool_size = int(os.getenv("DB_POOL_SIZE", "5"))
        pool_recycle = int(os.getenv("DB_POOL_RECYCLE", "1800"))

        logger.info(f"DB 엔진 초기화: pool_size={pool_size}")
        _engine = create_engine(
            url,
            pool_size=pool_size,
            pool_pre_ping=True,           # 끊긴 커넥션 자동 재연결
            pool_recycle=pool_recycle,    # 30분 후 커넥션 갱신 (MySQL wait_timeout 회피)
            future=True,                   # SQLAlchemy 2.0 스타일
            echo=False,
        )
        _SessionLocal = sessionmaker(
            bind=_engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )
    return _engine


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """
    분석 쿼리용 세션 컨텍스트.

    사용:
        with get_session() as session:
            rows = session.execute(text("SELECT ...")).mappings().all()

    분석 서비스는 read-only 라 트랜잭션 commit 불필요.
    예외 시 자동 rollback, 정상 시 자동 close.
    """
    if _SessionLocal is None:
        get_engine()  # 초기화

    session = _SessionLocal()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def close_engine() -> None:
    """앱 종료 시 호출 — 커넥션 풀 정리."""
    global _engine
    if _engine is not None:
        logger.info("DB 엔진 종료")
        _engine.dispose()
        _engine = None
