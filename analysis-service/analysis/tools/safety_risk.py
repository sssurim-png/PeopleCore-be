"""
#9 산업안전 위험 부서 진단 (detect_safety_risk_depts)

한국 근로기준법 명문 한도 적용 (사내 통상 비교 X):
  - 주 52시간 (근기법 §53)
  - 6일 연속근무 (근기법 §55, 주휴 보장)

1주 = 월~일 (회사 정책 고정).
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional, Dict, Any, List
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import text

from analysis.db import get_session
from analysis.tools._common import (
    get_company_id,
    base_result,
    insufficient_result,
)


logger = logging.getLogger("analysis.tools.safety_risk")


# ─── 주별 근무시간 집계 + 52h 위반 사원 ───
# 1주 = 월요일~일요일 (WEEKDAY 함수: 월=0, 일=6)
_WEEKLY_SQL = text("""
WITH emp_weekly AS (
    SELECT
        cr.emp_id,
        DATE_SUB(cr.work_date, INTERVAL WEEKDAY(cr.work_date) DAY) AS week_start,
        SUM(cr.actual_work_minutes) / 60.0 AS week_hours
    FROM commute_record cr
    JOIN v_active_employee ae ON ae.emp_id = cr.emp_id
    WHERE ae.company_id = UUID_TO_BIN(:cid)
      AND cr.work_date >= :start_date
      AND cr.work_date <  :end_date
    GROUP BY cr.emp_id, week_start
)
SELECT
    ew.emp_id,
    ae.dept_id,
    ae.dept_name,
    -- 위반 주 수 (52h 초과)
    SUM(CASE WHEN ew.week_hours >= :weekly_limit THEN 1 ELSE 0 END)             AS violation_weeks,
    -- 임계 근접 주 수 (50~52h)
    SUM(CASE WHEN ew.week_hours >= :near_threshold AND ew.week_hours < :weekly_limit
             THEN 1 ELSE 0 END)                                                  AS near_weeks,
    MAX(ew.week_hours)                                                           AS max_weekly_hours
FROM emp_weekly ew
JOIN v_active_employee ae ON ae.emp_id = ew.emp_id
GROUP BY ew.emp_id, ae.dept_id, ae.dept_name;
""")


# ─── 연속근무 일수 (work_date 기준 streak) ───
# 출근 또는 휴일근무 = 출근일로 카운트, ABSENT 만 제외
# work_date - ROW_NUMBER() 트릭으로 연속 그룹 식별
_CONSECUTIVE_SQL = text("""
WITH work_days AS (
    SELECT DISTINCT
        cr.emp_id,
        cr.work_date
    FROM commute_record cr
    JOIN v_active_employee ae ON ae.emp_id = cr.emp_id
    WHERE ae.company_id = UUID_TO_BIN(:cid)
      AND cr.work_date >= :start_date
      AND cr.work_date <  :end_date
      AND cr.work_status != 'ABSENT'
),
grouped AS (
    SELECT
        emp_id,
        work_date,
        DATE_SUB(work_date, INTERVAL ROW_NUMBER() OVER (PARTITION BY emp_id ORDER BY work_date) DAY) AS grp
    FROM work_days
),
streaks AS (
    SELECT
        emp_id,
        grp,
        COUNT(*) AS streak_length
    FROM grouped
    GROUP BY emp_id, grp
)
SELECT
    s.emp_id,
    ae.dept_id,
    ae.dept_name,
    MAX(s.streak_length)                                                AS max_consecutive_days,
    SUM(CASE WHEN s.streak_length >= :max_consecutive THEN 1 ELSE 0 END) AS violation_streaks
FROM streaks s
JOIN v_active_employee ae ON ae.emp_id = s.emp_id
GROUP BY s.emp_id, ae.dept_id, ae.dept_name;
""")


def _label_dept(
    violation_emp_count: int,
    near_emp_ratio: float,
    near_dept_threshold: float = 30.0,
) -> str:
    if violation_emp_count > 0:
        return "법규 위반 부서"
    if near_emp_ratio >= near_dept_threshold:
        return "임계 근접 부서"
    return "통상 범위"


def detect_safety_risk_depts(
    analysis_period_months: int = 3,
    weekly_hour_limit: float = 52.0,
    max_consecutive_work_days: int = 6,
    near_threshold_hours: float = 50.0,
    near_dept_emp_ratio: float = 30.0,
    min_dept_size: int = 10,
    company_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    한국 근로기준법 위반 부서 진단.

    Args:
        analysis_period_months: 집계 기간 (최근 N개월, 기본 3)
        weekly_hour_limit: 주간 근무시간 한도 (52h, 근기법 §53)
        max_consecutive_work_days: 연속근무 한도 (6일, 근기법 §55)
        near_threshold_hours: 임계 근접 판정 시간 (50h)
        near_dept_emp_ratio: 임계 근접 부서 판정 비율 (30%)
        min_dept_size: 분석 최소 부서 인원 (10)
        company_id: 회사 ID

    Returns:
        분석 결과 dict (위반 부서·사원 명단)
    """
    cid = get_company_id(company_id)
    end_date = date.today()
    start_date = end_date - timedelta(days=analysis_period_months * 30)

    params = {
        "analysis_period_months": analysis_period_months,
        "weekly_hour_limit": weekly_hour_limit,
        "max_consecutive_work_days": max_consecutive_work_days,
        "near_threshold_hours": near_threshold_hours,
        "min_dept_size": min_dept_size,
        "period": {
            "start_date": str(start_date),
            "end_date": str(end_date),
        },
    }

    season = {"season_id": None, "name": None, "start_date": "", "end_date": ""}

    # ─── 주별 + 연속근무 SQL 병렬 실행 ───
    def run_weekly():
        with get_session() as session:
            return session.execute(
                _WEEKLY_SQL,
                {
                    "cid": cid,
                    "start_date": start_date,
                    "end_date": end_date,
                    "weekly_limit": weekly_hour_limit,
                    "near_threshold": near_threshold_hours,
                },
            ).mappings().all()

    def run_consecutive():
        with get_session() as session:
            return session.execute(
                _CONSECUTIVE_SQL,
                {
                    "cid": cid,
                    "start_date": start_date,
                    "end_date": end_date,
                    "max_consecutive": max_consecutive_work_days + 1,
                },
            ).mappings().all()

    with ThreadPoolExecutor(max_workers=2) as ex:
        f_w = ex.submit(run_weekly)
        f_c = ex.submit(run_consecutive)
        weekly_rows = f_w.result()
        consec_rows = f_c.result()

    # 사원별 결과 합치기
    emp_data: Dict[int, Dict[str, Any]] = {}
    for r in weekly_rows:
        emp_data[r["emp_id"]] = {
            "emp_id": r["emp_id"],
            "dept_id": r["dept_id"],
            "dept_name": r["dept_name"],
            "violation_weeks": int(r["violation_weeks"] or 0),
            "near_weeks": int(r["near_weeks"] or 0),
            "max_weekly_hours": float(r["max_weekly_hours"] or 0.0),
            "max_consecutive_days": 0,
            "violation_streaks": 0,
        }

    for r in consec_rows:
        eid = r["emp_id"]
        if eid not in emp_data:
            emp_data[eid] = {
                "emp_id": eid,
                "dept_id": r["dept_id"],
                "dept_name": r["dept_name"],
                "violation_weeks": 0,
                "near_weeks": 0,
                "max_weekly_hours": 0.0,
                "max_consecutive_days": 0,
                "violation_streaks": 0,
            }
        emp_data[eid]["max_consecutive_days"] = int(r["max_consecutive_days"] or 0)
        emp_data[eid]["violation_streaks"] = int(r["violation_streaks"] or 0)

    # 위반 사원 명단 구성
    violation_emps: List[Dict[str, Any]] = []
    for emp in emp_data.values():
        types = []
        if emp["violation_weeks"] > 0:
            types.append("52h")
        if emp["violation_streaks"] > 0:
            types.append("연속근무")
        if types:
            emp["violation_types"] = types
            violation_emps.append(emp)

    # 부서별 집계
    dept_agg: Dict[int, Dict[str, Any]] = {}
    for emp in emp_data.values():
        dept_id = emp["dept_id"]
        if dept_id is None:
            continue
        if dept_id not in dept_agg:
            dept_agg[dept_id] = {
                "dept_id": dept_id,
                "dept_name": emp["dept_name"],
                "dept_n": 0,
                "violation_emp_count": 0,
                "near_emp_count": 0,
                "consec_violation_emp_count": 0,
                "max_consecutive_days": 0,
                "total_violation_weeks": 0,
            }
        d = dept_agg[dept_id]
        d["dept_n"] += 1
        if emp["violation_weeks"] > 0:
            d["violation_emp_count"] += 1
            d["total_violation_weeks"] += emp["violation_weeks"]
        if emp["near_weeks"] > 0 and emp["violation_weeks"] == 0:
            d["near_emp_count"] += 1
        if emp["violation_streaks"] > 0:
            d["consec_violation_emp_count"] += 1
        d["max_consecutive_days"] = max(d["max_consecutive_days"], emp["max_consecutive_days"])

    depts: List[Dict[str, Any]] = []
    label_counts: Dict[str, int] = {}

    for d in dept_agg.values():
        if d["dept_n"] < min_dept_size:
            continue
        violation_emp_total = d["violation_emp_count"] + d["consec_violation_emp_count"]
        near_ratio = d["near_emp_count"] * 100.0 / d["dept_n"] if d["dept_n"] > 0 else 0.0
        label = _label_dept(violation_emp_total, near_ratio, near_dept_emp_ratio)
        d["near_emp_ratio"] = round(near_ratio, 1)
        d["label"] = label
        label_counts[label] = label_counts.get(label, 0) + 1
        depts.append(d)

    if not depts:
        return insufficient_result(
            "I-09", "#9", season, params,
            reason="분석 기간 내 근태 데이터 없음 또는 모든 부서가 최소 인원 미달",
            extra_fields={"depts": [], "violation_employees": []},
        )

    return {
        **base_result("I-09", "#9", season, params),
        "summary": {
            "depts_analyzed": len(depts),
            "label_counts": label_counts,
            "total_violation_emps": len(violation_emps),
        },
        "depts": sorted(depts, key=lambda x: x["violation_emp_count"], reverse=True),
        "violation_employees": violation_emps,
    }
