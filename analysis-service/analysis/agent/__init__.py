"""
Multi-tool Agent — 한 발화로 분석 도구 N개를 자동 선택·실행·종합.

- planner: 사용자 발화 → 도구 N개 선택 + 실행 순서·의존성 결정
- executor: 도구 N개 실행 (병렬 + 순차)
- reasoner: 결과들 종합해서 교차 추론 narrative 생성

기존 단일 도구 흐름 (select_tool → execute_tool) 대체.
Planner 가 1개만 반환하면 자연스럽게 단일 흐름이 됨 (하위 호환).
"""
from analysis.agent.planner import plan_tools, ToolStep, ToolPlan
from analysis.agent.executor import execute_plan
from analysis.agent.reasoner import reason_results

__all__ = [
    "plan_tools",
    "ToolStep",
    "ToolPlan",
    "execute_plan",
    "reason_results",
]
