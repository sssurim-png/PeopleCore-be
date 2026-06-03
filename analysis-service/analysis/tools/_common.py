"""
분석 도구 공통 유틸리티.

분석 도구 7~8개에서 반복 사용되는 헬퍼:
  - 등급 → 점수 매핑
  - 분석 대상 시즌 결정 (None 이면 최신 CLOSED)
  - 회사 ID 결정 (.env 기본값 또는 인자)
  - 표본 크기에 따른 분석 모드 분류
"""
from __future__ import annotations

import os
import logging
from typing import Optional, List, Dict, Any
from sqlalchemy import text
from sqlalchemy.orm import Session


logger = logging.getLogger("analysis.tools.common")


# ─── 등급 매핑 ───
GRADE_TO_SCORE = {"S": 5, "A": 4, "B": 3, "C": 2, "D": 1}
SCORE_TO_GRADE = {v: k for k, v in GRADE_TO_SCORE.items()}


def grade_to_score(label: str) -> Optional[int]:
    """등급 문자열 → 점수. 매핑 외는 None (호출처에서 명시적 처리)."""
    return GRADE_TO_SCORE.get(label)


def score_to_grade(score: int) -> Optional[str]:
    """점수 → 등급 문자열. 매핑 외는 None (호출처에서 명시적 처리)."""
    return SCORE_TO_GRADE.get(score)


# ─── 회사 ID ───
def get_company_id(company_id: Optional[str] = None) -> str:
    """회사 ID 결정.

    - 인자가 있으면 그대로 사용 (multi-tenant 정상 경로)
    - 인자 없으면 .env 의 PEOPLECORE_COMPANY_ID 로 폴백 — 개발 환경 전용
    - 운영 환경(ENV=production)에선 폴백 차단. 다른 회사 데이터 노출 방지.
    """
    if company_id:
        return company_id

    if os.getenv("ENV", "").lower() == "production":
        raise RuntimeError(
            "운영 환경에서 company_id 인자 필수 — 인증 헤더 누락 의심"
        )

    cid = os.getenv("PEOPLECORE_COMPANY_ID")
    if not cid:
        raise RuntimeError("company_id 미설정 (인자 또는 PEOPLECORE_COMPANY_ID)")
    logger.warning("company_id 인자 없음 — .env 의 PEOPLECORE_COMPANY_ID 로 폴백 (개발용)")
    return cid


# ─── 시즌 결정 ───
def get_target_season(
    session: Session,
    company_id: str,
    season_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    분석 대상 시즌 결정.
    - season_id 인자 우선
    - 없으면 가장 최근 CLOSED 시즌
    """
    if season_id:
        sql = text("""
            SELECT season_id, name, period, start_date, end_date, status
            FROM season
            WHERE season_id = :sid AND company_id = UUID_TO_BIN(:cid)
        """)
        row = session.execute(sql, {"sid": season_id, "cid": company_id}).mappings().first()
    else:
        sql = text("""
            SELECT season_id, name, period, start_date, end_date, status
            FROM season
            WHERE company_id = UUID_TO_BIN(:cid)
              AND status = 'CLOSED'
            ORDER BY end_date DESC
            LIMIT 1
        """)
        row = session.execute(sql, {"cid": company_id}).mappings().first()

    if not row:
        # 로그엔 디버깅 정보, 사용자엔 일반 메시지
        logger.warning(
            f"시즌 없음 — season_id={season_id}, company_id={company_id}"
        )
        raise RuntimeError("분석 대상 시즌을 찾지 못했습니다.")
    return dict(row)


def get_recent_closed_seasons(
    session: Session,
    company_id: str,
    end_season_id: int,
    n: int,
) -> List[Dict[str, Any]]:
    """
    end_season_id 포함 직전 N 시즌 (CLOSED 만, 최신 → 과거 순).

    예: end_season_id=4, n=4 → [4, 3, 2, 1] (id 가 시즌 순서와 일치한다고 가정)
        실제론 end_date 기준 정렬.
    """
    # 먼저 end_season_id 의 end_date 조회
    end_sql = text("""
        SELECT end_date FROM season
        WHERE season_id = :sid AND company_id = UUID_TO_BIN(:cid)
    """)
    end_date = session.execute(end_sql, {"sid": end_season_id, "cid": company_id}).scalar_one()

    sql = text("""
        SELECT season_id, name, period, start_date, end_date, status
        FROM season
        WHERE company_id = UUID_TO_BIN(:cid)
          AND status = 'CLOSED'
          AND end_date <= :end_date
        ORDER BY end_date DESC
        LIMIT :n
    """)
    rows = session.execute(sql, {"cid": company_id, "end_date": end_date, "n": n}).mappings().all()
    return [dict(r) for r in rows]


# ─── 표본 크기 모드 분류 (#1, #5 등에서 사용) ───
def classify_sample_mode(
    n: int,
    min_full: int = 20,
    min_partial: int = 10,
    min_fallback: int = 5,
) -> str:
    """
    표본 크기 N 에 따른 분석 모드.

    - FULL:         N ≥ min_full       (정상 분석)
    - PARTIAL:      min_partial ≤ N < min_full   (신뢰도 제한적)
    - FALLBACK:     min_fallback ≤ N < min_partial  (참고 자료)
    - INSUFFICIENT: N < min_fallback   (분석 스킵)
    """
    if n >= min_full:
        return "FULL"
    if n >= min_partial:
        return "PARTIAL"
    if n >= min_fallback:
        return "FALLBACK"
    return "INSUFFICIENT"


# ─── 분석 결과 dict 표준 헬퍼 ───
def base_result(
    indicator_id: str,
    report_id: str,
    season: Dict[str, Any],
    params: Dict[str, Any],
) -> Dict[str, Any]:
    """모든 분석 도구가 반환하는 결과 dict 의 베이스."""
    return {
        "indicator_id": indicator_id,
        "report_id": report_id,
        "season": {
            "id": season.get("season_id"),
            "name": season.get("name"),
            "start_date": str(season.get("start_date", "")),
            "end_date": str(season.get("end_date", "")),
        },
        "params": params,
    }


# ─── 표본 부족 표준 응답 ───
INSUFFICIENT_MESSAGE = "분석 표본이 부족합니다"


def insufficient_result(
    indicator_id: str,
    report_id: str,
    season: Dict[str, Any],
    params: Dict[str, Any],
    reason: str,
    extra_fields: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    표본 부족 시 표준 응답.

    summary.mode = "INSUFFICIENT"
    summary.skipped_reason = "분석 표본이 부족합니다 — {reason}"

    extra_fields 로 각 도구의 빈 데이터 필드 포함 가능
    (예: candidates: [], depts: [] 등).
    """
    result = {
        **base_result(indicator_id, report_id, season, params),
        "summary": {
            "mode": "INSUFFICIENT",
            "skipped_reason": f"{INSUFFICIENT_MESSAGE} — {reason}",
        },
    }
    if extra_fields:
        result.update(extra_fields)
    return result
