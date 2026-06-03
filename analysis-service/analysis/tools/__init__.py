"""
분석 도구 패키지 — 분석 도구 9개의 공개 진입점.

사용:
    from analysis.tools import (
        find_underpaid_top_performers,           # I-02
        analyze_grade_distribution_by_dept,      # I-04
        find_evaluator_distribution_outliers,    # I-05
        find_employee_grade_volatility,          # I-06
        analyze_workload_balance,                # I-07
        detect_safety_risk_depts,                # I-09
        analyze_grade_salary_position,           # I-01
        generate_comprehensive_report,           # I-12
        analyze_relation_network,                # I-13  (Phase 4 — Neo4j)
    )

TOOL_REGISTRY 가 도구 메타·키워드의 single source of truth.
graph.py 의 intent 분류와 planner.py 의 도구 선택 모두 이걸 참조.
"""
from typing import Dict
from analysis.tools.underpaid_top import find_underpaid_top_performers
from analysis.tools.grade_dist_dept import analyze_grade_distribution_by_dept
from analysis.tools.evaluator_distribution import find_evaluator_distribution_outliers
from analysis.tools.grade_volatility import find_employee_grade_volatility
from analysis.tools.workload_balance import analyze_workload_balance
from analysis.tools.safety_risk import detect_safety_risk_depts
from analysis.tools.grade_salary_position import analyze_grade_salary_position
from analysis.tools.comprehensive import generate_comprehensive_report
from analysis.tools.relation_network import analyze_relation_network


# 분석 도구 메타 정보 — single source of truth.
# - func          : 실제 분석 함수
# - title         : 사람이 읽는 이름
# - report_id     : markdown 보고서 식별자
# - keywords      : 자연어 매칭용 (graph.classify_intent 와 planner._match_keyword 가 함께 참조)
# - description   : LLM Planner 가 LLM 호출 시 도구 설명용 (선택)
#
# 정의 순서 = 키워드 매칭 우선순위 (위에 있을수록 먼저 매칭).
# 좁은 키워드(I-13 "평가자 네트워크")가 넓은 키워드(I-05 "평가자")보다 위에 와야 함.
TOOL_REGISTRY = {
    # Phase 4 — Neo4j 관계 분석 (DB 관계 탐색 영역). 가장 위 = 우선순위 최상.
    "I-13": {
        "func": analyze_relation_network,
        "name": "analyze_relation_network",
        "title": "관계 네트워크 분석 (Neo4j)",
        "report_id": "#13",
        "description": "평가자 네트워크·조직도·상호평가·다중hop·부서×등급 분포 (그래프 DB)",
        "keywords": [
            "평가자 네트워크", "관계 네트워크", "조직도", "상호 평가", "상호평가",
            "평가자의 평가자", "부서 트리", "평가 관계", "평가 흐름",
        ],
    },
    "I-12": {
        "func": generate_comprehensive_report,
        "name": "generate_comprehensive_report",
        "title": "전사 진단 종합 보고서",
        "report_id": "#12",
        "keywords": ["종합", "전사 진단", "전체", "통합 보고서", "comprehensive"],
    },
    "I-09": {
        "func": detect_safety_risk_depts,
        "name": "detect_safety_risk_depts",
        "title": "산업안전 위험 부서 진단",
        "report_id": "#9",
        "keywords": ["산업안전", "근로기준법", "근기법", "52시간", "주52", "위반", "연속근무"],
    },
    "I-07": {
        "func": analyze_workload_balance,
        "name": "analyze_workload_balance",
        "title": "부서별 워라밸 진단",
        "report_id": "#7",
        "keywords": ["워라밸", "야근", "초과근무", "휴일근무", "연차", "휴가 사용"],
    },
    "I-05": {
        "func": find_evaluator_distribution_outliers,
        "name": "find_evaluator_distribution_outliers",
        "title": "평가자별 점수 분포 차이 진단",
        "report_id": "#5",
        "keywords": ["평가자", "편향", "managergrade", "manager grade", "평가 일관"],
    },
    "I-06": {
        "func": find_employee_grade_volatility,
        "name": "find_employee_grade_volatility",
        "title": "사원 등급 시계열 변동",
        "report_id": "#6",
        "keywords": ["등급 변동", "변동 패턴", "시계열", "안정 우수", "사원 패턴"],
    },
    "I-04": {
        "func": analyze_grade_distribution_by_dept,
        "name": "analyze_grade_distribution_by_dept",
        "title": "부서별 등급 분포 진단",
        "report_id": "#4",
        "keywords": ["부서별 등급", "부서 분포", "등급 분포", "부서 편중", "s+a 비율"],
    },
    "I-02": {
        "func": find_underpaid_top_performers,
        "name": "find_underpaid_top_performers",
        "title": "우수인재 보상 누락 발굴",
        "report_id": "#2",
        "keywords": ["보상 누락", "우수인재", "저연봉"],
    },
    "I-01": {
        "func": analyze_grade_salary_position,
        "name": "analyze_grade_salary_position",
        "title": "보상-성과 정합성 진단",
        "report_id": "#1",
        "keywords": ["보상-성과", "정합성", "변별력", "직급 변별", "부당 보상", "진급 정체"],
    },
}


def get_tool_keywords() -> Dict[str, list[str]]:
    """{indicator_id: [keywords]} — graph.py·planner.py 가 함께 사용하는 헬퍼."""
    return {tid: meta.get("keywords", []) for tid, meta in TOOL_REGISTRY.items()}


__all__ = [
    "find_underpaid_top_performers",
    "analyze_grade_distribution_by_dept",
    "find_evaluator_distribution_outliers",
    "find_employee_grade_volatility",
    "analyze_workload_balance",
    "detect_safety_risk_depts",
    "analyze_grade_salary_position",
    "generate_comprehensive_report",
    "analyze_relation_network",
    "TOOL_REGISTRY",
    "get_tool_keywords",
]
