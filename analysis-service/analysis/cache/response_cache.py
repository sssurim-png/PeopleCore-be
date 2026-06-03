"""
응답 캐싱 — 쿼리·회사ID 해시 기반 JSON 직렬화 캐시.

- 캐시 키: (정규화된 쿼리 + company_id) 의 sha256
- TTL: 환경변수 CACHE_TTL_SEC (기본 3600 = 1시간)
- 캐시 미적용 케이스:
    1. Redis 연결 실패
    2. HITL pending 상태 응답 (next_action 있음) — 사용자 결정 흐름이라 매번 새로
    3. error 응답 — 재시도 가능해야 하므로 캐싱 X
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from typing import Any, Dict, Optional

from analysis.cache.redis_client import get_redis


logger = logging.getLogger("analysis.cache.response")


_KEY_PREFIX = "analysis:resp:"
_DEFAULT_TTL_SEC = 3600  # 1시간


def _ttl_seconds() -> int:
    raw = os.getenv("CACHE_TTL_SEC", "").strip()
    try:
        return int(raw) if raw else _DEFAULT_TTL_SEC
    except ValueError:
        return _DEFAULT_TTL_SEC


def _normalize_query(query: str) -> str:
    """쿼리 정규화 — 공백·대소문자 차이만으로 캐시 미스 안 나게."""
    q = query.strip().lower()
    q = re.sub(r"\s+", " ", q)
    return q


def make_cache_key(query: str, company_id: Optional[str]) -> str:
    """캐시 키 생성 — sha256(normalized_query + company_id)."""
    payload = f"{_normalize_query(query)}|{company_id or ''}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{_KEY_PREFIX}{digest}"


def cache_get(query: str, company_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """캐시 조회. 없거나 Redis 미가용이면 None."""
    client = get_redis()
    if client is None:
        return None

    key = make_cache_key(query, company_id)
    try:
        raw = client.get(key)
    except Exception as e:
        logger.warning(f"cache_get 실패 ({e}) — 미스로 처리")
        return None

    if not raw:
        return None

    try:
        data = json.loads(raw)
        logger.info(f"cache HIT '{query[:30]}' (key={key[-12:]})")
        return data
    except json.JSONDecodeError:
        logger.warning(f"cache 역직렬화 실패 (key={key[-12:]})")
        return None


def cache_set(
    query: str,
    company_id: Optional[str],
    response: Dict[str, Any],
) -> None:
    """응답 캐싱. HITL pending / error 응답은 건너뜀."""
    client = get_redis()
    if client is None:
        return

    # error 는 캐싱 X (재시도 가능해야 함)
    # next_action(HITL) 은 캐싱 O — 첫 응답을 빠르게. resume 흐름은 매번 새로 진행.
    if response.get("error"):
        return

    key = make_cache_key(query, company_id)
    try:
        raw = json.dumps(response, ensure_ascii=False)
        client.setex(key, _ttl_seconds(), raw)
        logger.info(f"cache SET '{query[:30]}' (key={key[-12:]}, ttl={_ttl_seconds()}s)")
    except Exception as e:
        logger.warning(f"cache_set 실패 ({e})")
