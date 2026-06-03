"""
#6 사원 등급 시계열 변동 (find_employee_grade_volatility)

분석 흐름:
  1. 사원별 시즌별 finalGrade 시퀀스 수집 (최근 lookback 시즌)
  2. 시즌 수 < min_seasons 사원 제외 (신규 입사자)
  3. 시즌 수에 따라 분기:
     - 2시즌: 단순 비교 (유지/상승/하락)
     - 3+시즌: 풀 패턴 (안정 우수/평균/하위, 상승, 하락, 변동)
"""
from __future__ import annotations

import logging
import numpy as np
from typing import Optional, Dict, Any, List
from sqlalchemy import text, bindparam

from analysis.db import get_session
from analysis.tools._common import (
    get_company_id,
    get_target_season,
    get_recent_closed_seasons,
    base_result,
    insufficient_result,
    score_to_grade,
)


logger = logging.getLogger("analysis.tools.grade_volatility")


_SQL = text("""
SELECT
    ae.emp_id,
    ae.emp_name,
    ae.emp_num,
    ae.dept_id,
    ae.dept_name,
    ae.grade_id,
    ae.grade_name,
    GROUP_CONCAT(feg.final_grade       ORDER BY feg.season_start_date) AS grades_seq,
    GROUP_CONCAT(feg.final_grade_score ORDER BY feg.season_start_date) AS scores_seq,
    GROUP_CONCAT(feg.season_id         ORDER BY feg.season_start_date) AS season_ids_seq,
    COUNT(*)                                                            AS n_seasons,
    AVG(feg.final_grade_score)                                          AS mean_score,
    COALESCE(STDDEV_SAMP(feg.final_grade_score), 0)                     AS stdev_score
FROM v_active_employee ae
JOIN v_finalized_eval_grade feg ON feg.emp_id = ae.emp_id
WHERE ae.company_id = UUID_TO_BIN(:cid)
  AND feg.season_id IN :recent_season_ids
GROUP BY ae.emp_id, ae.emp_name, ae.emp_num, ae.dept_id, ae.dept_name, ae.grade_id, ae.grade_name
HAVING n_seasons >= :min_seasons
ORDER BY ae.emp_id;
""").bindparams(bindparam("recent_season_ids", expanding=True))


def _compute_slope(scores: List[float]) -> float:
    """등급 점수 시계열의 선형 회귀 기울기."""
    if len(scores) < 2:
        return 0.0
    xs = np.arange(len(scores), dtype=float)
    ys = np.array(scores, dtype=float)
    return float(np.polyfit(xs, ys, 1)[0])


def _perf_label(mean_score: float, high: float, low: float) -> str:
    """평균 점수 → 우수/평균/하위 라벨."""
    if mean_score >= high:
        return "우수"
    if mean_score <= low:
        return "하위"
    return "평균"


def _label_pattern(
    scores: List[int],
    n: int,
    mean_score: float,
    stdev_score: float,
    slope: float,
    stable_stdev: float,
    trend_slope: float,
    high_score: float,
    low_score: float,
) -> str:
    """패턴 라벨 부여."""
    if n == 2:
        if scores[0] == scores[1]:
            return f"유지 ({_perf_label(mean_score, high_score, low_score)})"
        return "상승" if scores[1] > scores[0] else "하락"

    # 3+ 시즌
    if stdev_score <= stable_stdev:
        return f"안정 {_perf_label(mean_score, high_score, low_score)}"

    if slope >= trend_slope:
        return "상승"
    if slope <= -trend_slope:
        return "하락"
    return "변동"


def find_employee_grade_volatility(
    season_id: Optional[int] = None,
    min_seasons: int = 2,
    lookback_seasons: int = 4,
    stable_stdev_threshold: float = 0.5,
    trend_slope_threshold: float = 0.3,
    high_grade_score: float = 4.0,
    low_grade_score: float = 2.0,
    company_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    사원 등급 시계열 변동 패턴 분석.

    Args:
        season_id: 분석 기준 시즌 (None = 최신 CLOSED)
        min_seasons: 분석 최소 활동 시즌 (2)
        lookback_seasons: 추적 시즌 수 (4)
        stable_stdev_threshold: 안정 판정 stdev 임계 (0.5)
        trend_slope_threshold: 상승/하락 판정 기울기 (0.3)
        high_grade_score: 우수 판정 평균 (4=A)
        low_grade_score: 하위 판정 평균 (2=C)
        company_id: 회사 ID

    Returns:
        분석 결과 dict
    """
    cid = get_company_id(company_id)
    params = {
        "season_id": season_id,
        "min_seasons": min_seasons,
        "lookback_seasons": lookback_seasons,
        "stable_stdev_threshold": stable_stdev_threshold,
        "trend_slope_threshold": trend_slope_threshold,
        "high_grade_score": high_grade_score,
        "low_grade_score": low_grade_score,
    }

    with get_session() as session:
        season = get_target_season(session, cid, season_id)
        recent = get_recent_closed_seasons(session, cid, season["season_id"], lookback_seasons)
        recent_ids = [s["season_id"] for s in recent]

        if len(recent_ids) < min_seasons:
            return insufficient_result(
                "I-06", "#6", season, params,
                reason=f"분석 시즌 {len(recent_ids)}개 (필요 {min_seasons}개 이상)",
                extra_fields={"employees": []},
            )

        rows = session.execute(
            _SQL,
            {
                "cid": cid,
                "recent_season_ids": recent_ids,
                "min_seasons": min_seasons,
            },
        ).mappings().all()

    employees: List[Dict[str, Any]] = []
    pattern_counts: Dict[str, int] = {}

    for r in rows:
        scores = [int(s) for s in (r["scores_seq"] or "").split(",") if s]
        grades = (r["grades_seq"] or "").split(",")
        n = int(r["n_seasons"] or 0)
        mean_score = float(r["mean_score"] or 0.0)
        stdev_score = float(r["stdev_score"] or 0.0)
        slope = _compute_slope(scores) if n >= 3 else 0.0

        pattern = _label_pattern(
            scores=scores,
            n=n,
            mean_score=mean_score,
            stdev_score=stdev_score,
            slope=slope,
            stable_stdev=stable_stdev_threshold,
            trend_slope=trend_slope_threshold,
            high_score=high_grade_score,
            low_score=low_grade_score,
        )
        pattern_counts[pattern] = pattern_counts.get(pattern, 0) + 1

        employees.append({
            "emp_id": r["emp_id"],
            "emp_name": r["emp_name"],
            "emp_num": r["emp_num"],
            "dept_id": r["dept_id"],
            "dept_name": r["dept_name"],
            "grade_id": r["grade_id"],
            "grade_name": r["grade_name"],
            "n_seasons": n,
            "grades_seq": grades,
            "scores_seq": scores,
            "mean_score": round(mean_score, 2),
            "stdev_score": round(stdev_score, 2),
            "trend_slope": round(slope, 2),
            "pattern": pattern,
        })

    # 분석 가능 사원이 0명 → 표본 부족
    if not employees:
        return insufficient_result(
            "I-06", "#6", season, params,
            reason=f"분석 가능한 사원 없음 (최소 {min_seasons}시즌 활동 사원 0명)",
            extra_fields={
                "lookback_seasons_used": [
                    {"id": s["season_id"], "name": s["name"]} for s in recent
                ],
                "employees": [],
            },
        )

    return {
        **base_result("I-06", "#6", season, params),
        "summary": {
            "total_analyzed": len(employees),
            "mode": "FULL",
            "lookback_seasons_used": [
                {"id": s["season_id"], "name": s["name"]} for s in recent
            ],
            "pattern_counts": pattern_counts,
        },
        "employees": employees,
    }
