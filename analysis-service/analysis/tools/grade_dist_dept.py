"""
#4 부서별 등급 분포 진단 (analyze_grade_distribution_by_dept)

분석 흐름:
  1. 분석 시즌 (None 이면 최신 CLOSED)
  2. 전사 S+A 비율 계산 (통상값)
  3. 부서별 S+A 비율 계산
  4. delta_sa = 부서 S+A − 전사 S+A
  5. 라벨 부여 (상위/하위 편중 / 통상)

매개변수:
  - min_dept_size: 10 (최소 부서 인원)
  - delta_threshold_pp: 10 (±%p 임계)
"""
from __future__ import annotations

import logging
from typing import Optional, Dict, Any, List
from sqlalchemy import text

from analysis.db import get_session
from analysis.tools._common import (
    get_company_id,
    get_target_season,
    base_result,
    insufficient_result,
)


logger = logging.getLogger("analysis.tools.grade_dist_dept")


_SQL = text("""
WITH season_emp AS (
    SELECT
        ae.emp_id, ae.dept_id, ae.dept_name, feg.final_grade
    FROM v_active_employee ae
    JOIN v_finalized_eval_grade feg ON feg.emp_id = ae.emp_id
    WHERE ae.company_id = UUID_TO_BIN(:cid)
      AND feg.season_id = :sid
),
company_dist AS (
    SELECT
        COUNT(*) AS total_n,
        SUM(CASE WHEN final_grade = 'S' THEN 1 ELSE 0 END) * 100.0 / COUNT(*)        AS s_ratio,
        SUM(CASE WHEN final_grade = 'A' THEN 1 ELSE 0 END) * 100.0 / COUNT(*)        AS a_ratio,
        SUM(CASE WHEN final_grade IN ('S','A') THEN 1 ELSE 0 END) * 100.0 / COUNT(*) AS sa_ratio
    FROM season_emp
),
dept_dist AS (
    SELECT
        dept_id, dept_name,
        COUNT(*)                                                                     AS dept_n,
        SUM(CASE WHEN final_grade = 'S' THEN 1 ELSE 0 END) * 100.0 / COUNT(*)        AS s_ratio_dept,
        SUM(CASE WHEN final_grade = 'A' THEN 1 ELSE 0 END) * 100.0 / COUNT(*)        AS a_ratio_dept,
        SUM(CASE WHEN final_grade IN ('S','A') THEN 1 ELSE 0 END) * 100.0 / COUNT(*) AS sa_ratio_dept
    FROM season_emp
    GROUP BY dept_id, dept_name
)
SELECT
    d.dept_id,
    d.dept_name,
    d.dept_n,
    ROUND(d.s_ratio_dept, 1)              AS s_ratio_dept,
    ROUND(d.a_ratio_dept, 1)              AS a_ratio_dept,
    ROUND(d.sa_ratio_dept, 1)             AS sa_ratio_dept,
    ROUND(c.sa_ratio, 1)                  AS sa_ratio_company,
    ROUND(d.sa_ratio_dept - c.sa_ratio, 1) AS delta_sa,
    c.total_n                             AS company_total_n
FROM dept_dist d
CROSS JOIN company_dist c
ORDER BY (d.sa_ratio_dept - c.sa_ratio) DESC;
""")


def _label(delta_sa: float, dept_n: int, min_dept_size: int, threshold_pp: float) -> str:
    if dept_n < min_dept_size:
        return "표본 부족"
    if delta_sa >= threshold_pp:
        return "상위 편중"
    if delta_sa <= -threshold_pp:
        return "하위 편중"
    return "통상 범위"


def analyze_grade_distribution_by_dept(
    season_id: Optional[int] = None,
    min_dept_size: int = 10,
    delta_threshold_pp: float = 10.0,
    company_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    부서별 등급 분포 진단.

    Args:
        season_id: 분석 시즌 (None = 최신 CLOSED)
        min_dept_size: 분석 최소 부서 인원 (기본 10)
        delta_threshold_pp: 전사 대비 편중 판정 임계치 (기본 ±10%p)
        company_id: 회사 ID (None = .env 기본값)

    Returns:
        분석 결과 dict
    """
    cid = get_company_id(company_id)
    params = {
        "season_id": season_id,
        "min_dept_size": min_dept_size,
        "delta_threshold_pp": delta_threshold_pp,
    }

    with get_session() as session:
        season = get_target_season(session, cid, season_id)
        rows = session.execute(_SQL, {"cid": cid, "sid": season["season_id"]}).mappings().all()

    if not rows:
        return insufficient_result(
            "I-04", "#4", season, params,
            reason="분석 시즌에 등급 데이터 없음",
            extra_fields={"company": None, "depts": []},
        )

    company_total_n = int(rows[0]["company_total_n"] or 0)
    sa_ratio_company = float(rows[0]["sa_ratio_company"] or 0.0)

    depts: List[Dict[str, Any]] = []
    top_count = 0
    bottom_count = 0
    skipped_count = 0

    for r in rows:
        dept_n = int(r["dept_n"] or 0)
        delta_sa = float(r["delta_sa"] or 0.0)
        label = _label(delta_sa, dept_n, min_dept_size, delta_threshold_pp)

        if label == "상위 편중":
            top_count += 1
        elif label == "하위 편중":
            bottom_count += 1
        elif label == "표본 부족":
            skipped_count += 1

        depts.append({
            "dept_id": r["dept_id"],
            "dept_name": r["dept_name"],
            "n": dept_n,
            "s_ratio": float(r["s_ratio_dept"] or 0.0),
            "a_ratio": float(r["a_ratio_dept"] or 0.0),
            "sa_ratio": float(r["sa_ratio_dept"] or 0.0),
            "delta_sa": delta_sa,
            "label": label,
        })

    # 모든 부서가 표본 부족 → 전체 INSUFFICIENT 처리
    if depts and skipped_count == len(depts):
        return insufficient_result(
            "I-04", "#4", season, params,
            reason=f"모든 {len(depts)}개 부서가 최소 인원({min_dept_size}명) 미달",
            extra_fields={
                "company": {"total_n": company_total_n, "sa_ratio": sa_ratio_company},
                "depts": depts,
            },
        )

    return {
        **base_result("I-04", "#4", season, params),
        "summary": {
            "total_employees": company_total_n,
            "company_sa_ratio": sa_ratio_company,
            "depts_analyzed": len(depts),
            "depts_top_concentrated": top_count,
            "depts_bottom_concentrated": bottom_count,
            "depts_skipped_size": skipped_count,
        },
        "company": {
            "total_n": company_total_n,
            "sa_ratio": sa_ratio_company,
        },
        "depts": depts,
    }
