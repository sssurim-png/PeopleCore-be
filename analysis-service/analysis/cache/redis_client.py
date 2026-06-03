"""
Redis 클라이언트 — 싱글톤 + 환경변수 기반.

REDIS_URL 미설정이면 캐싱 기능 자체를 비활성화 (None 반환).
"""
from __future__ import annotations

import logging
import os
from typing import Optional

try:
    import redis  # type: ignore
except ImportError:
    redis = None  # type: ignore


logger = logging.getLogger("analysis.cache.redis")


_client: Optional["redis.Redis"] = None
_init_tried: bool = False


def get_redis() -> Optional["redis.Redis"]:
    """Redis 싱글톤 반환. 연결 불가 시 None (호출 측에서 우회)."""
    global _client, _init_tried

    if _client is not None or _init_tried:
        return _client
    _init_tried = True

    if redis is None:
        logger.warning("redis 패키지 미설치 — 캐싱 비활성")
        return None

    url = os.getenv("REDIS_URL", "").strip()
    if not url:
        logger.info("REDIS_URL 미설정 — 캐싱 비활성")
        return None

    try:
        client = redis.from_url(url, decode_responses=True, socket_connect_timeout=2)
        client.ping()
        _client = client
        logger.info(f"redis 연결 OK ({url})")
        return _client
    except Exception as e:
        logger.warning(f"redis 연결 실패 → 캐싱 비활성: {e}")
        return None


def close_redis() -> None:
    """앱 종료 시 정리."""
    global _client, _init_tried
    if _client is not None:
        try:
            _client.close()
        except Exception:
            pass
    _client = None
    _init_tried = False
