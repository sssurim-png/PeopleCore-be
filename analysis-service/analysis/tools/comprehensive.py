"""
#12 전사 진단 종합 보고서 (generate_comprehensive_report)

#1, #2, #4, #5, #6, #7, #9 결과를 5개 섹션으로 통합:
  §1. 전사 요약
  §2. 부서별 매트릭스
  §3. 사원 후보 통합 (결합 강도)
  §4. 평가 진단 (#5 상세)
  §5. 한계 + 권고 액션 (우선순위)
"""
from __future__ import annotations

import logging
from typing import Optional, Dict, Any, List
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from analysis.tools._common import base_result, get_company_id, get_target_season
from analysis.db import get_session
from analysis.tools.underpaid_top import find_underpaid_top_performers
from analysis.tools.grade_dist_dept import analyze_grade_distribution_by_dept
from analysis.tools.evaluator_distribution import find_evaluator_distribution_outliers
from analysis.tools.grade_volatility import find_employee_grade_volatility
from analysis.tools.workload_balance import analyze_workload_balance
from analysis.tools.safety_risk import detect_safety_risk_depts
from analysis.tools.grade_salary_position import analyze_grade_salary_position


logger = logging.getLogger("analysis.tools.comprehensive")


def _build_dept_matrix(
    grade_dist: Dict[str, Any],
    workload: Dict[str, Any],
    safety: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """§2 부서별 매트릭스 — #4, #7, #9 라벨 통합."""
    matrix: Dict[int, Dict[str, Any]] = {}

    # #4 부서 등급 분포
    for d in grade_dist.get("depts", []):
        matrix[d["dept_id"]] = {
            "dept_id": d["dept_id"],
            "dept_name": d["dept_name"],
            "label_grade_dist": d["label"],
            "label_workload": "통상 범위",
            "label_safety": "통상 범위",
        }

    # #7 워라밸
    for d in workload.get("depts", []):
        if d["dept_id"] not in matrix:
            matrix[d["dept_id"]] = {
                "dept_id": d["dept_id"],
                "dept_name": d["dept_name"],
                "label_grade_dist": "데이터 없음",
                "label_workload": "통상 범위",
                "label_safety": "통상 범위",
            }
        matrix[d["dept_id"]]["label_workload"] = " + ".join(d.get("labels", ["통상 범위"]))

    # #9 산업안전
    for d in safety.get("depts", []):
        if d["dept_id"] not in matrix:
            matrix[d["dept_id"]] = {
                "dept_id": d["dept_id"],
                "dept_name": d["dept_name"],
                "label_grade_dist": "데이터 없음",
                "label_workload": "데이터 없음",
                "label_safety": "통상 범위",
            }
        matrix[d["dept_id"]]["label_safety"] = d["label"]

    return list(matrix.values())


def _build_emp_candidates(
    underpaid: Dict[str, Any],
    salary_position: Dict[str, Any],
    volatility: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    §3 사원 후보 통합 — #2, #1-3, #6 의 사원 후보 합집합.
    결합 강도 = 동시 도출된 분석 수.
    """
    emp_data: Dict[int, Dict[str, Any]] = defaultdict(lambda: {
        "in_underpaid": False,
        "in_unfair": False,
        "in_volatility_pattern": None,
    })

    # #2 우수인재 보상 누락
    for c in underpaid.get("candidates", []):
        emp_data[c["emp_id"]].update({
            "emp_id": c["emp_id"],
            "emp_name": c["emp_name"],
            "dept_name": c["dept_name"],
            "grade_name": c["grade_name"],
            "in_underpaid": True,
            "underpaid_pctile": c.get("salary_pctile"),
        })

    # #1-3 부당 보상 후보
    for c in salary_position.get("detail3_unfair_candidates", []):
        emp_data[c["emp_id"]].update({
            "emp_id": c["emp_id"],
            "emp_name": c["emp_name"],
            "dept_name": c["dept_name"],
            "grade_name": c["grade_name"],
            "in_unfair": True,
            "unfair_case": c.get("case"),
        })

    # #6 등급 변동 — 안정 우수, 안정 하위, 상승, 하락 패턴만 통합 (변동·안정 평균 제외)
    significant_patterns = {"안정 우수", "안정 하위", "상승", "하락", "유지 (우수)", "유지 (하위)"}
    for e in volatility.get("employees", []):
        if e.get("pattern") in significant_patterns:
            emp_data[e["emp_id"]].update({
                "emp_id": e["emp_id"],
                "emp_name": e["emp_name"],
                "dept_name": e["dept_name"],
                "grade_name": e["grade_name"],
                "in_volatility_pattern": e["pattern"],
            })

    # 결합 강도 계산
    candidates = []
    for emp in emp_data.values():
        signals = []
        if emp.get("in_underpaid"):
            signals.append("#2 보상 누락")
        if emp.get("in_unfair"):
            signals.append(f"#1-3 부당 (케이스 {emp.get('unfair_case')})")
        if emp.get("in_volatility_pattern"):
            signals.append(f"#6 {emp['in_volatility_pattern']}")

        n = len(signals)
        if n == 0:
            continue
        if n >= 3:
            strength = "강함"
        elif n == 2:
            strength = "중간"
        else:
            strength = "단일"

        candidates.append({
            "emp_id": emp["emp_id"],
            "emp_name": emp.get("emp_name", "?"),
            "dept_name": emp.get("dept_name", "?"),
            "grade_name": emp.get("grade_name", "?"),
            "signals": signals,
            "n_signals": n,
            "strength": strength,
        })

    # 강한 시그널 우선 정렬
    return sorted(candidates, key=lambda x: -x["n_signals"])


def generate_comprehensive_report(
    season_id: Optional[int] = None,
    lookback_seasons: int = 4,
    analysis_period_months: int = 3,
    company_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    #1~#9 일괄 호출 + 5섹션 통합 보고서 생성.

    Args:
        season_id: 분석 시즌 (None = 최신 CLOSED)
        lookback_seasons: 다년 분석 시즌 수 (4)
        analysis_period_months: 근태 분석 기간 (3개월)
        company_id: 회사 ID

    Returns:
        통합 보고서 dict (5섹션)
    """
    cid = get_company_id(company_id)
    params = {
        "season_id": season_id,
        "lookback_seasons": lookback_seasons,
        "analysis_period_months": analysis_period_months,
    }

    # 시즌 결정 (모든 분석에 동일 시즌 사용)
    with get_session() as session:
        season = get_target_season(session, cid, season_id)
    target_season_id = season["season_id"]

    logger.info(f"종합 보고서 시작 — season={target_season_id}")

    # ─── 7개 분석 병렬 실행 (ThreadPoolExecutor) ───
    # 각 분석은 자체 DB 세션을 열어 독립적으로 동작 → 스레드 안전
    tasks = {
        "grade_dist": (analyze_grade_distribution_by_dept,
                       {"season_id": target_season_id, "company_id": cid}),
        "underpaid": (find_underpaid_top_performers,
                      {"season_id": target_season_id, "company_id": cid}),
        "evaluator": (find_evaluator_distribution_outliers,
                      {"season_id": target_season_id, "company_id": cid}),
        "volatility": (find_employee_grade_volatility,
                       {"season_id": target_season_id,
                        "lookback_seasons": lookback_seasons,
                        "company_id": cid}),
        "workload": (analyze_workload_balance,
                     {"analysis_period_months": analysis_period_months,
                      "company_id": cid}),
        "safety": (detect_safety_risk_depts,
                   {"analysis_period_months": analysis_period_months,
                    "company_id": cid}),
        "salary_pos": (analyze_grade_salary_position,
                       {"season_id": target_season_id,
                        "lookback_seasons": lookback_seasons,
                        "company_id": cid}),
    }

    results: Dict[str, Dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=len(tasks)) as ex:
        futures = {ex.submit(func, **kwargs): name for name, (func, kwargs) in tasks.items()}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                results[name] = fut.result()
                logger.info(f"  ✓ {name} 완료")
            except Exception as e:
                logger.exception(f"  ✗ {name} 실패: {e}")
                results[name] = {"error": str(e), "summary": {}}

    grade_dist = results["grade_dist"]
    underpaid = results["underpaid"]
    evaluator = results["evaluator"]
    volatility = results["volatility"]
    workload = results["workload"]
    safety = results["safety"]
    salary_pos = results["salary_pos"]

    logger.info(f"종합 보고서 — 7개 분석 병렬 완료")

    # §1 전사 요약
    summary_overview = {
        "season": {"id": target_season_id, "name": season.get("name")},
        "evaluation_discrimination": {
            "label_count_by_grade": {
                d["grade_name"]: d.get("label_short_term")
                for d in salary_pos.get("detail1_short_term_discrimination", [])
            },
        },
        "dept_grade_distribution": grade_dist.get("summary", {}),
        "evaluator_pattern": {
            "mode": evaluator.get("summary", {}).get("mode"),
            "n_evaluators": evaluator.get("summary", {}).get("n_evaluators"),
            "n_candidates": evaluator.get("summary", {}).get("n_candidates"),
        },
        "workload": workload.get("summary", {}),
        "safety": safety.get("summary", {}),
        "underpaid_count": underpaid.get("summary", {}).get("total_candidates", 0),
        "unfair_count": salary_pos.get("summary", {}).get("detail3_candidates", 0),
    }

    # §2 부서별 매트릭스
    dept_matrix = _build_dept_matrix(grade_dist, workload, safety)

    # §3 사원 후보 통합
    emp_candidates = _build_emp_candidates(underpaid, salary_pos, volatility)

    # §4 평가 진단 (#5)
    evaluator_section = {
        "mode": evaluator.get("summary", {}).get("mode"),
        "n_evaluators": evaluator.get("summary", {}).get("n_evaluators"),
        "candidates": evaluator.get("candidates", []),
    }

    # §5 한계 + 권고 우선순위
    priorities = []
    # 1순위: 법규 위반
    if safety.get("summary", {}).get("total_violation_emps", 0) > 0:
        priorities.append({
            "level": 1,
            "category": "법규 위반",
            "summary": f"{safety['summary']['total_violation_emps']}명 위반 — 즉시 시정 검토",
            "details": safety.get("violation_employees", [])[:5],
        })
    # 2순위: 강 시그널 사원
    strong = [c for c in emp_candidates if c["strength"] == "강함"]
    if strong:
        priorities.append({
            "level": 2,
            "category": "강 시그널 사원",
            "summary": f"결합 강도 강함 {len(strong)}명 — 즉시 면담 우선",
            "details": strong[:5],
        })
    # 2순위 계속: 과부하 부서
    overload = [d for d in workload.get("depts", []) if "과부하" in (d.get("labels") or [])]
    if overload:
        priorities.append({
            "level": 2,
            "category": "과부하 부서",
            "summary": f"{len(overload)}개 부서 — 부담 분산 검토",
            "details": [{"dept_name": d["dept_name"]} for d in overload[:3]],
        })
    # 3순위: 중 시그널 사원
    medium = [c for c in emp_candidates if c["strength"] == "중간"]
    if medium:
        priorities.append({
            "level": 3,
            "category": "중 시그널 사원",
            "summary": f"결합 강도 중간 {len(medium)}명 — 검토 안건",
            "details": medium[:5],
        })
    # 3순위 계속: 편중 부서
    concentrated = [d for d in dept_matrix if "편중" in d.get("label_grade_dist", "")]
    if concentrated:
        priorities.append({
            "level": 3,
            "category": "등급 편중 부서",
            "summary": f"{len(concentrated)}개 부서 — #5 결합 후 평가자 패턴 확인",
            "details": concentrated[:3],
        })

    # 모드 정보 (신뢰도)
    mode_info = {
        "evaluator_mode": evaluator.get("summary", {}).get("mode"),
        "salary_pos_modes": [
            {
                "grade_name": d["grade_name"],
                "mode": d.get("mode"),
                "n": d["n"],
            }
            for d in salary_pos.get("detail1_short_term_discrimination", [])
        ],
    }

    return {
        **base_result("I-12", "#12", season, params),
        "summary_overview": summary_overview,
        "section_2_dept_matrix": dept_matrix,
        "section_3_emp_candidates": emp_candidates,
        "section_4_evaluator": evaluator_section,
        "section_5_priorities": priorities,
        "mode_info": mode_info,
        "source_analyses": {
            # 각 sub-분석의 summary 외에 FE/markdown 에서 자세히 보일 detail 도 함께.
            # 종합 보고서 안에서 직급별 변별력·부당 보상 후보·등급 변동 패턴 등을
            # 사용자가 한 화면에서 확인할 수 있게.
            "I-01": {
                "summary": salary_pos.get("summary"),
                "detail1_short_term_discrimination": salary_pos.get("detail1_short_term_discrimination", []),
                "detail3_unfair_candidates": salary_pos.get("detail3_unfair_candidates", []),
            },
            "I-02": {
                "summary": underpaid.get("summary"),
                "candidates": underpaid.get("candidates", []),
            },
            "I-04": {
                "summary": grade_dist.get("summary"),
                "depts": grade_dist.get("depts", []),
                "company": grade_dist.get("company"),
            },
            "I-05": {
                "summary": evaluator.get("summary"),
                "candidates": evaluator.get("candidates", []),
            },
            "I-06": {
                "summary": volatility.get("summary"),
                "employees": volatility.get("employees", []),
            },
            "I-07": {
                "summary": workload.get("summary"),
                "depts": workload.get("depts", []),
            },
            "I-09": {
                "summary": safety.get("summary"),
                "depts": safety.get("depts", []),
                "violation_employees": safety.get("violation_employees", []),
            },
        },
    }
