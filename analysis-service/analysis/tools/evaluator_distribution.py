"""
#5 평가자별 점수 분포 차이 진단 (find_evaluator_distribution_outliers)

managerGrade (1차 상위평가) 기준, 표본 크기 자동 모드 전환:
  - FULL (≥20):       백분위 + Z-score + 변별력
  - ZSCORE (10~19):   Z-score + 변별력
  - FALLBACK (5~9):   상위·하위 N명 + 변별력
  - INSUFFICIENT (<5): 스킵 + 명시 메시지

※ 다년 추적·turnover 보강은 v2 작업으로 미룸.
   현재는 분석 시즌 단발 분석.
"""
from __future__ import annotations

import logging
from typing import Optional, Dict, Any, List
from sqlalchemy import text

from analysis.db import get_session
from analysis.tools._common import (
    get_company_id,
    get_target_season,
    classify_sample_mode,
    base_result,
)


logger = logging.getLogger("analysis.tools.evaluator_distribution")


_SQL = text("""
WITH evaluator_stats AS (
    SELECT
        me.evaluator_id,
        e.emp_name                                                              AS evaluator_name,
        e.dept_id,
        d.dept_name,
        COUNT(*)                                                                AS n_evaluatees,
        AVG(me.grade_score)                                                     AS mean_score,
        SUM(CASE WHEN me.grade_label IN ('S','A') THEN 1 ELSE 0 END) * 100.0 / COUNT(*)
                                                                                AS sa_ratio,
        COALESCE(STDDEV_SAMP(me.grade_score), 0)                                AS stdev_score
    FROM v_manager_evaluation me
    JOIN employee e   ON e.emp_id = me.evaluator_id
    JOIN department d ON d.dept_id = e.dept_id
    WHERE me.season_id = :sid
      AND e.company_id = UUID_TO_BIN(:cid)
    GROUP BY me.evaluator_id, e.emp_name, e.dept_id, d.dept_name
    HAVING n_evaluatees >= :min_evaluatees
),
company_stats AS (
    SELECT
        AVG(mean_score)                              AS company_mean_score,
        COALESCE(STDDEV_SAMP(mean_score), 0)         AS company_stdev_mean,
        AVG(sa_ratio)                                AS company_mean_sa,
        COALESCE(STDDEV_SAMP(sa_ratio), 0)           AS company_stdev_sa,
        COUNT(*)                                     AS n_evaluators
    FROM evaluator_stats
)
SELECT
    es.evaluator_id,
    es.evaluator_name,
    es.dept_id,
    es.dept_name,
    es.n_evaluatees,
    ROUND(es.mean_score, 2)                                                      AS mean_score,
    ROUND(es.sa_ratio, 1)                                                        AS sa_ratio,
    ROUND(es.stdev_score, 3)                                                     AS stdev_score,
    cs.n_evaluators,
    ROUND(cs.company_mean_score, 2)                                              AS company_mean_score,
    ROUND(cs.company_mean_sa, 1)                                                 AS company_mean_sa,
    -- Z-score (분모 0 보호)
    ROUND(
        CASE WHEN cs.company_stdev_mean > 0
             THEN (es.mean_score - cs.company_mean_score) / cs.company_stdev_mean
             ELSE 0 END, 2)                                                      AS mean_z,
    ROUND(
        CASE WHEN cs.company_stdev_sa > 0
             THEN (es.sa_ratio - cs.company_mean_sa) / cs.company_stdev_sa
             ELSE 0 END, 2)                                                      AS sa_z,
    -- 백분위 (FULL 모드용)
    ROUND(PERCENT_RANK() OVER (ORDER BY es.mean_score) * 100, 1)                 AS mean_pctile,
    ROUND(PERCENT_RANK() OVER (ORDER BY es.sa_ratio)  * 100, 1)                  AS sa_pctile
FROM evaluator_stats es
CROSS JOIN company_stats cs
ORDER BY es.mean_score DESC;
""")


def _flag_reasons(
    row: Dict[str, Any],
    mode: str,
    z_threshold: float,
    low_variance: float,
) -> List[str]:
    """평가자 1차 필터 — 모드별. 이유 리스트 반환 (빈 리스트면 통상 범위)."""
    reasons: List[str] = []

    # 변별력 — 모든 모드 공통
    if row["stdev_score"] <= low_variance:
        reasons.append("변별력 낮음")

    if mode in ("FULL", "ZSCORE"):
        # Z-score 양 끝
        if row["mean_z"] >= z_threshold:
            reasons.append(f"상위 (Z={row['mean_z']:+.2f})")
        elif row["mean_z"] <= -z_threshold:
            reasons.append(f"하위 (Z={row['mean_z']:+.2f})")

    if mode == "FULL":
        # 백분위 양 끝
        if row["mean_pctile"] >= 90:
            reasons.append("상위 백분위 (≥90%ile)")
        elif row["mean_pctile"] <= 10:
            reasons.append("하위 백분위 (≤10%ile)")

    # FALLBACK 의 상위/하위 N명 도출은 본 함수가 아니라 호출 측에서 처리
    return reasons


def find_evaluator_distribution_outliers(
    season_id: Optional[int] = None,
    min_evaluatees: int = 5,
    min_evaluators_for_full: int = 20,
    min_evaluators_for_zscore: int = 10,
    min_evaluators_for_fallback: int = 5,
    z_score_threshold: float = 1.5,
    fallback_top_n: int = 1,
    low_variance_threshold: float = 0.3,
    company_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    평가자 점수 분포 차이 진단.

    Args:
        season_id: 분석 시즌 (None = 최신 CLOSED)
        min_evaluatees: 평가자가 평가한 최소 사원 수 (소표본 제외)
        min_evaluators_for_full: FULL 모드 진입 풀 크기
        min_evaluators_for_zscore: ZSCORE 모드 진입 풀 크기
        min_evaluators_for_fallback: FALLBACK 모드 진입 풀 크기
        z_score_threshold: Z-score 임계 (±1.5σ)
        fallback_top_n: FALLBACK 모드에서 상위·하위 N명 도출
        low_variance_threshold: 변별력 낮음 stdev 절대 임계 (0.3)
        company_id: 회사 ID

    Returns:
        분석 결과 dict (모드 명시)
    """
    cid = get_company_id(company_id)
    params = {
        "season_id": season_id,
        "min_evaluatees": min_evaluatees,
        "z_score_threshold": z_score_threshold,
        "fallback_top_n": fallback_top_n,
        "low_variance_threshold": low_variance_threshold,
    }

    with get_session() as session:
        season = get_target_season(session, cid, season_id)
        rows = session.execute(
            _SQL,
            {
                "cid": cid,
                "sid": season["season_id"],
                "min_evaluatees": min_evaluatees,
            },
        ).mappings().all()

    n_evaluators = len(rows)
    mode = classify_sample_mode(
        n_evaluators,
        min_full=min_evaluators_for_full,
        min_partial=min_evaluators_for_zscore,
        min_fallback=min_evaluators_for_fallback,
    )
    # FULL/PARTIAL/FALLBACK/INSUFFICIENT → 우리 의미로 매핑
    mode_map = {
        "FULL": "FULL",
        "PARTIAL": "ZSCORE",
        "FALLBACK": "FALLBACK",
        "INSUFFICIENT": "INSUFFICIENT",
    }
    mode = mode_map[mode]

    if mode == "INSUFFICIENT":
        return {
            **base_result("I-05", "#5", season, params),
            "summary": {
                "n_evaluators": n_evaluators,
                "mode": "INSUFFICIENT",
                "skipped_reason": (
                    f"평가자 풀 N={n_evaluators} (5명 미만). "
                    "통계 분석 의미 확보 어려움. 정성 검토 권고."
                ),
            },
            "evaluators": [],
            "candidates": [],
        }

    # 평가자 결과 정리
    evaluators: List[Dict[str, Any]] = []
    candidates: List[Dict[str, Any]] = []

    for r in rows:
        record = dict(r)
        reasons = _flag_reasons(record, mode, z_score_threshold, low_variance_threshold)
        record["flag_reasons"] = reasons
        evaluators.append(record)

        if reasons:
            candidates.append(record)

    # FALLBACK 모드: Z-score/백분위 후보가 없거나 적으면 상위·하위 N명 강제 도출
    if mode == "FALLBACK" and not any("상위" in r or "하위" in r
                                       for c in candidates
                                       for r in c["flag_reasons"]):
        sorted_by_mean = sorted(evaluators, key=lambda x: x["mean_score"])
        # 하위 N
        for r in sorted_by_mean[:fallback_top_n]:
            r["flag_reasons"] = list(r["flag_reasons"]) + ["하위 (FALLBACK 절대 도출)"]
            if r not in candidates:
                candidates.append(r)
        # 상위 N
        for r in sorted_by_mean[-fallback_top_n:]:
            r["flag_reasons"] = list(r["flag_reasons"]) + ["상위 (FALLBACK 절대 도출)"]
            if r not in candidates:
                candidates.append(r)

    return {
        **base_result("I-05", "#5", season, params),
        "summary": {
            "n_evaluators": n_evaluators,
            "mode": mode,
            "n_candidates": len(candidates),
            "company_mean_score": float(rows[0]["company_mean_score"]) if rows else None,
            "company_mean_sa": float(rows[0]["company_mean_sa"]) if rows else None,
        },
        "evaluators": evaluators,
        "candidates": candidates,
    }
