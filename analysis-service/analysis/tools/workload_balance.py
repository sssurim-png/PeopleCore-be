"""
#7 부서별 워라밸 진단 (analyze_workload_balance)

근태(`CommuteRecord`) + 휴가(`VacationBalance`) 데이터로 부서별 부담 진단.

라벨 (부서 한 곳에 복수 부착 가능):
  - 과부하 부서: 야근/야간/휴일 백분위 ≥ 90
  - 연차 미사용: 사용률 ≤ 50% (절대)
  - 무급 야근 의심: 미인정 초과 ≥ 50% (절대)
  - 통상 범위
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
    get_target_season,
    base_result,
    insufficient_result,
)


logger = logging.getLogger("analysis.tools.workload_balance")


# ─── 근태 집계 SQL ───
_COMMUTE_SQL = text("""
WITH dept_commute AS (
    SELECT
        ae.dept_id,
        ae.dept_name,
        COUNT(DISTINCT ae.emp_id) AS dept_n,
        -- 1인당 월평균 야근 분
        COALESCE(SUM(cr.overtime_minutes), 0) * 1.0
            / NULLIF(COUNT(DISTINCT ae.emp_id) * :months, 0) AS mean_overtime_per_emp,
        -- 1인당 월평균 야간근무 분
        COALESCE(SUM(cr.recognized_night_minutes), 0) * 1.0
            / NULLIF(COUNT(DISTINCT ae.emp_id) * :months, 0) AS mean_night_per_emp,
        -- 1인당 월평균 휴일근무 분
        COALESCE(SUM(cr.recognized_holiday_minutes), 0) * 1.0
            / NULLIF(COUNT(DISTINCT ae.emp_id) * :months, 0) AS mean_holiday_per_emp,
        -- 미인정 초과 비율 (전체 초과 중 미인정)
        CASE WHEN SUM(cr.overtime_minutes) > 0
             THEN SUM(cr.unrecognized_ot_minutes) * 100.0 / SUM(cr.overtime_minutes)
             ELSE 0 END AS unrecognized_ot_ratio,
        -- 휴일 출근 발생 사원 비율
        COUNT(DISTINCT CASE
            WHEN cr.holiday_reason IS NOT NULL AND cr.actual_work_minutes > 0
            THEN ae.emp_id
        END) * 100.0 / NULLIF(COUNT(DISTINCT ae.emp_id), 0) AS holiday_work_ratio,
        -- 지각일 비율
        SUM(CASE WHEN cr.work_status IN ('LATE', 'LATE_AND_EARLY') THEN 1 ELSE 0 END) * 100.0
            / NULLIF(SUM(CASE WHEN cr.work_status IS NOT NULL THEN 1 ELSE 0 END), 0) AS late_day_ratio
    FROM v_active_employee ae
    LEFT JOIN commute_record cr ON cr.emp_id = ae.emp_id
        AND cr.work_date >= :start_date
        AND cr.work_date < :end_date
    WHERE ae.company_id = UUID_TO_BIN(:cid)
    GROUP BY ae.dept_id, ae.dept_name
    HAVING dept_n >= :min_dept_size
)
SELECT
    dept_id, dept_name, dept_n,
    ROUND(mean_overtime_per_emp, 1)                                        AS mean_overtime_per_emp,
    ROUND(mean_night_per_emp,    1)                                        AS mean_night_per_emp,
    ROUND(mean_holiday_per_emp,  1)                                        AS mean_holiday_per_emp,
    ROUND(unrecognized_ot_ratio, 1)                                        AS unrecognized_ot_ratio,
    ROUND(holiday_work_ratio,    1)                                        AS holiday_work_ratio,
    ROUND(late_day_ratio,        2)                                        AS late_day_ratio,
    ROUND(PERCENT_RANK() OVER (ORDER BY mean_overtime_per_emp) * 100, 1)  AS overtime_pctile,
    ROUND(PERCENT_RANK() OVER (ORDER BY mean_night_per_emp)    * 100, 1)  AS night_pctile,
    ROUND(PERCENT_RANK() OVER (ORDER BY holiday_work_ratio)    * 100, 1)  AS holiday_pctile
FROM dept_commute
ORDER BY mean_overtime_per_emp DESC;
""")


# ─── 연차 사용률 집계 SQL (StatutoryVacationType.ANNUAL 한정) ───
_VACATION_SQL = text("""
SELECT
    ae.dept_id,
    AVG(
        CASE WHEN vb.total_days > 0
             THEN vb.used_days * 100.0 / vb.total_days
             ELSE 0 END
    )                                                            AS mean_leave_usage_rate,
    AVG(
        CASE WHEN vb.total_days > 0
             THEN vb.expired_days * 100.0 / vb.total_days
             ELSE 0 END
    )                                                            AS mean_expired_ratio
FROM v_active_employee ae
JOIN vacation_balance vb ON vb.emp_id = ae.emp_id
JOIN vacation_type vt   ON vt.type_id = vb.type_id
WHERE ae.company_id = UUID_TO_BIN(:cid)
  AND vt.type_code = 'ANNUAL'
  AND vb.balance_year = :balance_year
GROUP BY ae.dept_id;
""")


def _build_labels(
    row: Dict[str, Any],
    overtime_pctile_thresh: float,
    low_leave_thresh: float,
    high_unrec_ot_thresh: float,
) -> List[str]:
    labels: List[str] = []

    # 과부하 — 세 지표 중 하나라도 상위 백분위
    if (row["overtime_pctile"] >= overtime_pctile_thresh
        or row["night_pctile"] >= overtime_pctile_thresh
        or row["holiday_pctile"] >= overtime_pctile_thresh):
        labels.append("과부하 부서")

    # 무급 야근 의심
    if row["unrecognized_ot_ratio"] >= high_unrec_ot_thresh:
        labels.append("무급 야근 의심")

    # 연차 미사용
    leave_rate = row.get("mean_leave_usage_rate")
    if leave_rate is not None and leave_rate <= low_leave_thresh:
        labels.append("연차 미사용 부서")

    if not labels:
        labels.append("통상 범위")
    return labels


def analyze_workload_balance(
    analysis_period_months: int = 3,
    min_dept_size: int = 10,
    dept_top_pctile: float = 90.0,
    low_leave_usage_threshold: float = 50.0,
    high_unrecognized_ot_threshold: float = 50.0,
    company_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    부서별 워라밸 진단.

    Args:
        analysis_period_months: 집계 기간 (최근 N개월, 기본 3)
        min_dept_size: 분석 최소 부서 인원 (10)
        dept_top_pctile: 과부하 판정 백분위 (90)
        low_leave_usage_threshold: 연차 미사용 절대 임계 (50%)
        high_unrecognized_ot_threshold: 무급 야근 의심 절대 임계 (50%)
        company_id: 회사 ID

    Returns:
        분석 결과 dict
    """
    cid = get_company_id(company_id)
    end_date = date.today()
    start_date = end_date - timedelta(days=analysis_period_months * 30)
    balance_year = end_date.year

    params = {
        "analysis_period_months": analysis_period_months,
        "min_dept_size": min_dept_size,
        "dept_top_pctile": dept_top_pctile,
        "low_leave_usage_threshold": low_leave_usage_threshold,
        "high_unrecognized_ot_threshold": high_unrecognized_ot_threshold,
        "period": {
            "start_date": str(start_date),
            "end_date": str(end_date),
        },
    }

    # 시즌 정보 (가벼움)
    with get_session() as session:
        try:
            season = get_target_season(session, cid, None)
        except RuntimeError:
            season = {"season_id": None, "name": None, "start_date": "", "end_date": ""}

    # ─── 근태 + 휴가 SQL 병렬 실행 ───
    def run_commute():
        with get_session() as session:
            return session.execute(
                _COMMUTE_SQL,
                {
                    "cid": cid,
                    "months": analysis_period_months,
                    "start_date": start_date,
                    "end_date": end_date,
                    "min_dept_size": min_dept_size,
                },
            ).mappings().all()

    def run_vacation():
        try:
            with get_session() as session:
                rows = session.execute(
                    _VACATION_SQL,
                    {"cid": cid, "balance_year": balance_year},
                ).mappings().all()
            return {
                r["dept_id"]: {
                    "mean_leave_usage_rate": float(r["mean_leave_usage_rate"] or 0.0),
                    "mean_expired_ratio": float(r["mean_expired_ratio"] or 0.0),
                }
                for r in rows
            }
        except Exception as e:
            logger.warning(f"휴가 데이터 조회 실패: {e}")
            return {}

    with ThreadPoolExecutor(max_workers=2) as ex:
        f_commute = ex.submit(run_commute)
        f_vacation = ex.submit(run_vacation)
        commute_rows = f_commute.result()
        vacation_by_dept = f_vacation.result()

    if not commute_rows:
        return insufficient_result(
            "I-07", "#7", season, params,
            reason="분석 기간 내 근태 데이터 없음 또는 모든 부서가 최소 인원 미달",
            extra_fields={"depts": []},
        )

    depts: List[Dict[str, Any]] = []
    label_counts: Dict[str, int] = {}

    for r in commute_rows:
        record = dict(r)

        # 휴가 정보 결합
        vac = vacation_by_dept.get(record["dept_id"], {})
        record["mean_leave_usage_rate"] = vac.get("mean_leave_usage_rate")
        record["mean_expired_ratio"] = vac.get("mean_expired_ratio")

        record["labels"] = _build_labels(
            record,
            overtime_pctile_thresh=dept_top_pctile,
            low_leave_thresh=low_leave_usage_threshold,
            high_unrec_ot_thresh=high_unrecognized_ot_threshold,
        )
        for label in record["labels"]:
            label_counts[label] = label_counts.get(label, 0) + 1

        depts.append(record)

    return {
        **base_result("I-07", "#7", season, params),
        "summary": {
            "depts_analyzed": len(depts),
            "label_counts": label_counts,
            "vacation_data_available": bool(vacation_by_dept),
        },
        "depts": depts,
    }
