"""
#2 우수인재 보상 누락 발굴 (find_underpaid_top_performers)

조건 (모두 AND):
  1. 최근 N시즌 동안 모두 S 또는 A 등급 (기본 N=2)
  2. 같은 직급(grade_id) 내 연봉 백분위 ≤ 25%
  3. 직급 내 분석 풀 ≥ 10명

연봉 정의: 분석 시즌(반기 = 6개월) 동안의 SALARY + BONUS 합계.

※ pay_year_month 컬럼 형식 가정: 'YYYY-MM' (예: '2025-07')
   실제 DB 와 다르면 _SQL 의 BETWEEN 부분 수정 필요.
"""
from __future__ import annotations

import logging
from typing import Optional, Dict, Any, List
from sqlalchemy import text, bindparam

from analysis.db import get_session
from analysis.tools._common import (
    get_company_id,
    get_target_season,
    get_recent_closed_seasons,
    base_result,
    insufficient_result,
)


logger = logging.getLogger("analysis.tools.underpaid_top")


# ─── 메인 SQL ───
_SQL = text("""
WITH emp_recent_grades AS (
    SELECT
        feg.emp_id,
        ae.grade_id,
        ae.grade_order,
        ae.grade_name,
        ae.dept_id,
        ae.dept_name,
        ae.emp_name,
        ae.emp_num,
        ae.tenure_years,
        COUNT(*)                                                        AS season_count,
        SUM(CASE WHEN feg.final_grade IN ('S','A') THEN 1 ELSE 0 END)   AS sa_count,
        GROUP_CONCAT(feg.final_grade ORDER BY feg.season_start_date)    AS grades_seq
    FROM v_active_employee ae
    JOIN v_finalized_eval_grade feg ON feg.emp_id = ae.emp_id
    WHERE ae.company_id = UUID_TO_BIN(:cid)
      AND feg.season_id IN :recent_season_ids
    GROUP BY feg.emp_id, ae.grade_id, ae.grade_order, ae.grade_name,
             ae.dept_id, ae.dept_name, ae.emp_name, ae.emp_num, ae.tenure_years
    HAVING season_count >= :n_seasons
       AND sa_count = season_count
),
emp_season_pay AS (
    SELECT
        p.emp_id,
        SUM(p.total_amount) AS season_pay
    FROM v_employee_payroll_by_category p
    WHERE p.company_id = UUID_TO_BIN(:cid)
      AND p.category IN ('SALARY', 'BONUS')
      AND p.pay_year_month BETWEEN :start_ym AND :end_ym
    GROUP BY p.emp_id
),
ranked AS (
    SELECT
        erg.emp_id,
        erg.grade_id,
        erg.grade_order,
        erg.grade_name,
        erg.dept_id,
        erg.dept_name,
        erg.emp_name,
        erg.emp_num,
        erg.tenure_years,
        erg.grades_seq,
        COALESCE(esp.season_pay, 0)                                       AS season_pay,
        PERCENT_RANK() OVER (PARTITION BY erg.grade_id ORDER BY COALESCE(esp.season_pay, 0)) * 100
                                                                          AS salary_pctile,
        COUNT(*)         OVER (PARTITION BY erg.grade_id)                 AS pool_size
    FROM emp_recent_grades erg
    LEFT JOIN emp_season_pay esp ON esp.emp_id = erg.emp_id
)
SELECT
    emp_id, emp_name, emp_num,
    grade_id, grade_name, grade_order,
    dept_id, dept_name,
    tenure_years, grades_seq,
    season_pay,
    ROUND(salary_pctile, 1) AS salary_pctile,
    pool_size
FROM ranked
WHERE salary_pctile <= :threshold
  AND pool_size >= :min_pool
ORDER BY salary_pctile ASC, season_pay ASC;
""").bindparams(bindparam("recent_season_ids", expanding=True))


def find_underpaid_top_performers(
    season_id: Optional[int] = None,
    min_seasons_a: int = 2,
    salary_percentile_threshold: float = 25.0,
    min_pool_size: int = 10,
    company_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    우수인재 보상 누락 후보 도출.

    Args:
        season_id: 분석 대상 시즌 (None 이면 최신 CLOSED)
        min_seasons_a: 최소 A 이상 등급 시즌 수 (기본 2)
        salary_percentile_threshold: 직급 내 백분위 임계 (기본 25)
        min_pool_size: 직급 분석 최소 인원 (기본 10)
        company_id: 회사 ID (None 이면 .env 기본값)

    Returns:
        분석 결과 dict (베이스 + summary + candidates)
    """
    cid = get_company_id(company_id)
    params = {
        "season_id": season_id,
        "min_seasons_a": min_seasons_a,
        "salary_percentile_threshold": salary_percentile_threshold,
        "min_pool_size": min_pool_size,
    }

    with get_session() as session:
        season = get_target_season(session, cid, season_id)
        recent = get_recent_closed_seasons(session, cid, season["season_id"], min_seasons_a)
        recent_ids = [s["season_id"] for s in recent]

        if len(recent_ids) < min_seasons_a:
            return insufficient_result(
                "I-02", "#2", season, params,
                reason=f"분석 시즌 {len(recent_ids)}개 (필요 {min_seasons_a}개 이상)",
                extra_fields={"candidates": []},
            )

        # 분석 시즌(반기)의 pay_year_month 범위
        start_ym = season["start_date"].strftime("%Y-%m")
        end_ym = season["end_date"].strftime("%Y-%m")

        rows = session.execute(
            _SQL,
            {
                "cid": cid,
                "recent_season_ids": recent_ids,
                "n_seasons": min_seasons_a,
                "start_ym": start_ym,
                "end_ym": end_ym,
                "threshold": salary_percentile_threshold,
                "min_pool": min_pool_size,
            },
        ).mappings().all()

    candidates: List[Dict[str, Any]] = []
    for r in rows:
        candidates.append({
            "emp_id": r["emp_id"],
            "emp_name": r["emp_name"],
            "emp_num": r["emp_num"],
            "grade_id": r["grade_id"],
            "grade_name": r["grade_name"],
            "grade_order": r["grade_order"],
            "dept_id": r["dept_id"],
            "dept_name": r["dept_name"],
            "tenure_years": int(r["tenure_years"] or 0),
            "grades_seq": (r["grades_seq"] or "").split(","),
            "season_pay": int(r["season_pay"] or 0),
            "salary_pctile": float(r["salary_pctile"] or 0.0),
            "pool_size": int(r["pool_size"] or 0),
        })

    return {
        **base_result("I-02", "#2", season, params),
        "summary": {
            "total_candidates": len(candidates),
            "mode": "FULL" if candidates else "EMPTY",
            "analyzed_seasons": [
                {"id": s["season_id"], "name": s["name"]} for s in recent
            ],
        },
        "candidates": candidates,
    }
