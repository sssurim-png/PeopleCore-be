"""
#1 보상-성과 정합성 진단 (analyze_grade_salary_position)

3개 세부 진단:
  세부 1 — 단기 변별력: 단년 등급 ↔ 성과급(BONUS) 피어슨 상관 (직급 단위)
  세부 2 — 장기 변별력: 다년 등급 평균 ↔ 기본급(SALARY) 인상률 (직급 단위)
  세부 3 — 부당 보상 후보 (사원 단위, 케이스 A or B)

표본 크기 자동 모드 전환 (FULL/PARTIAL/WEAK/INSUFFICIENT) — 직급별 적용.
"""
from __future__ import annotations

import logging
import numpy as np
from typing import Optional, Dict, Any, List, Tuple
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import text, bindparam

from analysis.db import get_session
from analysis.tools._common import (
    get_company_id,
    get_target_season,
    get_recent_closed_seasons,
    classify_sample_mode,
    base_result,
)


logger = logging.getLogger("analysis.tools.grade_salary_position")


# ─── 세부 1: 단기 변별력 — 직급별 (등급, 성과급) 데이터 ───
_DETAIL1_SQL = text("""
SELECT
    ae.grade_id,
    ae.grade_name,
    ae.grade_order,
    ae.emp_id,
    feg.final_grade_score AS grade_score,
    COALESCE(SUM(CASE WHEN p.category = 'BONUS' THEN p.total_amount ELSE 0 END), 0) AS bonus_amount
FROM v_active_employee ae
JOIN v_finalized_eval_grade feg ON feg.emp_id = ae.emp_id
LEFT JOIN v_employee_payroll_by_category p ON p.emp_id = ae.emp_id
    AND p.category = 'BONUS'
    AND p.pay_year_month BETWEEN :start_ym AND :end_ym
WHERE ae.company_id = UUID_TO_BIN(:cid)
  AND feg.season_id = :sid
GROUP BY ae.grade_id, ae.grade_name, ae.grade_order, ae.emp_id, feg.final_grade_score
ORDER BY ae.grade_order, ae.emp_id;
""")


# ─── 세부 3: 부당 보상 후보 발굴 (사원 단위) ───
# 공통 + (케이스 A OR 케이스 B) 조건
_DETAIL3_SQL = text("""
WITH emp_metrics AS (
    SELECT
        ae.emp_id,
        ae.emp_name,
        ae.emp_num,
        ae.dept_id,
        ae.dept_name,
        ae.grade_id,
        ae.grade_name,
        ae.grade_order,
        ae.tenure_years,
        AVG(feg.final_grade_score)              AS avg_grade_score,
        MIN(feg.final_grade_score)              AS min_grade_score,
        GROUP_CONCAT(feg.final_grade ORDER BY feg.season_start_date) AS grades_seq,
        COUNT(*)                                 AS n_seasons
    FROM v_active_employee ae
    JOIN v_finalized_eval_grade feg ON feg.emp_id = ae.emp_id
    WHERE ae.company_id = UUID_TO_BIN(:cid)
      AND feg.season_id IN :recent_season_ids
    GROUP BY ae.emp_id, ae.emp_name, ae.emp_num, ae.dept_id, ae.dept_name,
             ae.grade_id, ae.grade_name, ae.grade_order, ae.tenure_years
),
last_promotion AS (
    SELECT emp_id, MAX(effective_date) AS last_promo_date
    FROM v_promotion_history
    WHERE company_id = UUID_TO_BIN(:cid)
    GROUP BY emp_id
),
emp_attendance AS (
    SELECT
        ae.emp_id,
        SUM(CASE WHEN cr.work_status IN ('LATE', 'LATE_AND_EARLY', 'ABSENT') THEN 1 ELSE 0 END)
            * 1.0 / NULLIF(COUNT(*), 0) AS bad_ratio
    FROM v_active_employee ae
    LEFT JOIN commute_record cr ON cr.emp_id = ae.emp_id
        AND cr.work_date >= DATE_SUB(CURRENT_DATE, INTERVAL 6 MONTH)
    WHERE ae.company_id = UUID_TO_BIN(:cid)
    GROUP BY ae.emp_id
),
emp_pay_pctile AS (
    SELECT
        ae.emp_id,
        ae.grade_id,
        SUM(CASE WHEN p.category IN ('SALARY', 'BONUS') THEN p.total_amount ELSE 0 END) AS season_pay
    FROM v_active_employee ae
    LEFT JOIN v_employee_payroll_by_category p ON p.emp_id = ae.emp_id
        AND p.category IN ('SALARY', 'BONUS')
        AND p.pay_year_month BETWEEN :start_ym AND :end_ym
    WHERE ae.company_id = UUID_TO_BIN(:cid)
    GROUP BY ae.emp_id, ae.grade_id
),
ranked_pay AS (
    SELECT
        emp_id,
        grade_id,
        season_pay,
        PERCENT_RANK() OVER (PARTITION BY grade_id ORDER BY season_pay) * 100 AS salary_pctile
    FROM emp_pay_pctile
)
SELECT
    em.*,
    COALESCE(TIMESTAMPDIFF(YEAR, lp.last_promo_date, CURRENT_DATE), em.tenure_years) AS years_since_last_promotion,
    COALESCE(ea.bad_ratio, 0.0)                                                       AS bad_ratio,
    -- attendance_score_pctile: bad_ratio 의 역수 백분위 (낮을수록 양호)
    100 - COALESCE(
        (SELECT 100.0 * (RANK() OVER (ORDER BY bad_ratio) - 1) /
                NULLIF((SELECT COUNT(*) FROM emp_attendance) - 1, 0)
         FROM emp_attendance e2 WHERE e2.emp_id = em.emp_id),
        50.0
    ) AS attendance_score_pctile,
    rp.salary_pctile,
    rp.season_pay
FROM emp_metrics em
LEFT JOIN last_promotion  lp ON lp.emp_id = em.emp_id
LEFT JOIN emp_attendance  ea ON ea.emp_id = em.emp_id
LEFT JOIN ranked_pay      rp ON rp.emp_id = em.emp_id
ORDER BY em.emp_id;
""").bindparams(bindparam("recent_season_ids", expanding=True))


def _grade_label_to_score(label: str) -> int:
    return {"S": 5, "A": 4, "B": 3, "C": 2, "D": 1}.get(label, 0)


# ─── 세부 1 분석 — 직급별 피어슨 상관 ───
def _analyze_detail1(session, cid: str, season_id: int, start_ym: str, end_ym: str,
                     min_full: int, min_partial: int, min_skip: int,
                     weak_thresh: float, normal_thresh: float) -> List[Dict[str, Any]]:
    rows = session.execute(
        _DETAIL1_SQL,
        {"cid": cid, "sid": season_id, "start_ym": start_ym, "end_ym": end_ym},
    ).mappings().all()

    # 직급별로 그룹핑
    by_grade: Dict[int, Dict[str, Any]] = {}
    for r in rows:
        gid = r["grade_id"]
        if gid not in by_grade:
            by_grade[gid] = {
                "grade_id": gid,
                "grade_name": r["grade_name"],
                "grade_order": r["grade_order"],
                "scores": [],
                "bonuses": [],
            }
        by_grade[gid]["scores"].append(int(r["grade_score"]))
        by_grade[gid]["bonuses"].append(float(r["bonus_amount"]))

    results: List[Dict[str, Any]] = []
    for gid, data in by_grade.items():
        n = len(data["scores"])
        mode = classify_sample_mode(n, min_full=min_full, min_partial=min_partial, min_fallback=min_skip)
        if mode == "INSUFFICIENT":
            results.append({
                "grade_id": gid,
                "grade_name": data["grade_name"],
                "grade_order": data["grade_order"],
                "n": n,
                "mode": "INSUFFICIENT",
                "corr_grade_bonus": None,
                "label_short_term": "표본 부족",
            })
            continue

        if np.std(data["scores"]) == 0 or np.std(data["bonuses"]) == 0:
            corr = 0.0
        else:
            corr = float(np.corrcoef(data["scores"], data["bonuses"])[0, 1])

        if corr >= normal_thresh:
            label = "정상"
        elif corr < weak_thresh:
            label = "변별력 약함"
        else:
            label = "약함 경계"

        results.append({
            "grade_id": gid,
            "grade_name": data["grade_name"],
            "grade_order": data["grade_order"],
            "n": n,
            "mode": mode,
            "corr_grade_bonus": round(corr, 3),
            "label_short_term": label,
        })

    return sorted(results, key=lambda x: x["grade_order"])


# ─── 세부 3 분석 — 부당 보상 후보 ───
def _check_unfair(
    row: Dict[str, Any],
    min_tenure: int,
    typical_promo: int,
    overdue_buf: int,
    attend_top: float,
    case_a_avg: float, case_a_floor_score: int,
    case_b_avg: float, case_b_floor_score: int,
    low_salary: float,
) -> Tuple[bool, Optional[str]]:
    """후보 여부 + 케이스 라벨."""
    # 공통
    if row["tenure_years"] < min_tenure:
        return False, None
    if (row["attendance_score_pctile"] or 0.0) < attend_top:
        return False, None
    if (row["salary_pctile"] or 100.0) > low_salary:
        return False, None

    yspromo = row["years_since_last_promotion"] or 0
    avg_grade = row["avg_grade_score"] or 0
    min_grade = row["min_grade_score"] or 0

    # 케이스 A: 통상자
    if (yspromo >= (typical_promo + overdue_buf)
        and avg_grade >= case_a_avg
        and min_grade >= case_a_floor_score):
        return True, "A"

    # 케이스 B: 우수자
    if (yspromo >= typical_promo
        and avg_grade >= case_b_avg
        and min_grade >= case_b_floor_score):
        return True, "B"

    return False, None


def analyze_grade_salary_position(
    season_id: Optional[int] = None,
    lookback_seasons: int = 4,
    # 세부 1·2 변별력
    min_pool_for_full: int = 20,
    min_pool_for_partial: int = 10,
    min_pool_for_skip: int = 5,
    weak_corr_threshold: float = 0.3,
    normal_corr_threshold: float = 0.5,
    # 세부 3 부당 보상
    min_tenure_years: int = 5,
    typical_promotion_years: int = 4,
    overdue_buffer_years: int = 2,
    attendance_top_pctile: float = 70.0,
    case_a_avg_grade: float = 3.0,
    case_a_grade_floor: str = "B",
    case_b_avg_grade: float = 4.0,
    case_b_grade_floor: str = "A",
    low_salary_pctile: float = 30.0,
    company_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    보상-성과 정합성 진단.

    Returns:
        분석 결과 dict (세부 1·3 통합. 세부 2 는 v2 작업)
    """
    cid = get_company_id(company_id)
    params = {
        "season_id": season_id,
        "lookback_seasons": lookback_seasons,
        "min_tenure_years": min_tenure_years,
        "typical_promotion_years": typical_promotion_years,
        "overdue_buffer_years": overdue_buffer_years,
        "attendance_top_pctile": attendance_top_pctile,
        "case_a_avg_grade": case_a_avg_grade,
        "case_a_grade_floor": case_a_grade_floor,
        "case_b_avg_grade": case_b_avg_grade,
        "case_b_grade_floor": case_b_grade_floor,
        "low_salary_pctile": low_salary_pctile,
    }

    case_a_floor_score = _grade_label_to_score(case_a_grade_floor)
    case_b_floor_score = _grade_label_to_score(case_b_grade_floor)

    # 시즌 결정 (단일 세션, 가벼운 쿼리)
    with get_session() as session:
        season = get_target_season(session, cid, season_id)
        recent = get_recent_closed_seasons(session, cid, season["season_id"], lookback_seasons)
        recent_ids = [s["season_id"] for s in recent]

    start_ym = season["start_date"].strftime("%Y-%m")
    end_ym = season["end_date"].strftime("%Y-%m")

    # ─── 세부 1 + 세부 3 SQL 병렬 실행 ───
    def run_detail1():
        with get_session() as session:
            return _analyze_detail1(
                session, cid, season["season_id"], start_ym, end_ym,
                min_pool_for_full, min_pool_for_partial, min_pool_for_skip,
                weak_corr_threshold, normal_corr_threshold,
            )

    def run_detail3():
        with get_session() as session:
            return session.execute(
                _DETAIL3_SQL,
                {
                    "cid": cid,
                    "recent_season_ids": recent_ids,
                    "start_ym": start_ym,
                    "end_ym": end_ym,
                },
            ).mappings().all()

    with ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(run_detail1)
        f3 = ex.submit(run_detail3)
        detail1 = f1.result()
        rows = f3.result()

    candidates: List[Dict[str, Any]] = []
    case_counts = {"A": 0, "B": 0}
    for r in rows:
        record = dict(r)
        is_unfair, case = _check_unfair(
            record,
            min_tenure=min_tenure_years,
            typical_promo=typical_promotion_years,
            overdue_buf=overdue_buffer_years,
            attend_top=attendance_top_pctile,
            case_a_avg=case_a_avg_grade,
            case_a_floor_score=case_a_floor_score,
            case_b_avg=case_b_avg_grade,
            case_b_floor_score=case_b_floor_score,
            low_salary=low_salary_pctile,
        )
        if is_unfair:
            case_counts[case] += 1
            candidates.append({
                "emp_id": record["emp_id"],
                "emp_name": record["emp_name"],
                "emp_num": record["emp_num"],
                "dept_id": record["dept_id"],
                "dept_name": record["dept_name"],
                "grade_id": record["grade_id"],
                "grade_name": record["grade_name"],
                "tenure_years": int(record["tenure_years"] or 0),
                "years_since_last_promotion": int(record["years_since_last_promotion"] or 0),
                "grades_seq": (record["grades_seq"] or "").split(","),
                "avg_grade_score": round(float(record["avg_grade_score"] or 0.0), 2),
                "min_grade_score": int(record["min_grade_score"] or 0),
                "attendance_score_pctile": round(float(record["attendance_score_pctile"] or 0.0), 1),
                "salary_pctile": round(float(record["salary_pctile"] or 0.0), 1),
                "season_pay": int(record["season_pay"] or 0),
                "case": case,
            })

    return {
        **base_result("I-01", "#1", season, params),
        "summary": {
            "lookback_seasons_used": [{"id": s["season_id"], "name": s["name"]} for s in recent],
            "detail1_short_term_grades": len(detail1),
            "detail3_candidates": len(candidates),
            "case_counts": case_counts,
        },
        "detail1_short_term_discrimination": detail1,
        "detail3_unfair_candidates": candidates,
    }
