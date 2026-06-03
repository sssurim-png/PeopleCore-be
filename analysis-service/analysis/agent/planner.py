"""
Planner — 사용자 발화를 분석해 어떤 도구를 어떤 순서로 실행할지 결정.

기존 select_tool 은 도구 1개만 선택했지만, Planner 는 도구 N개를 조합 가능.
LLM 이 JSON 으로 ToolPlan 을 반환하면 Executor 가 그대로 실행.

예시:
  query: "워라밸 위험 부서 중 보상 누락도 있는 곳"
  plan:  [
    ToolStep("I-07", depends_on=None),
    ToolStep("I-02", depends_on="I-07", param_mapping={"dept_ids": "depts.risky"})
  ]
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from analysis.tools import TOOL_REGISTRY, get_tool_keywords


logger = logging.getLogger("analysis.agent.planner")


# ─── 키워드 → 단일 도구 매핑 (LLM 우회 빠른 경로) ───
# 단순 질문은 즉시 도구 1개로 매핑해 LLM 호출 없이 빠르게 처리.
# TOOL_REGISTRY 가 single source of truth — 변경은 tools/__init__.py 에서.
_QUICK_KEYWORDS = get_tool_keywords()

# 복합 질문 시그널 — 이 단어가 있으면 키워드 매칭 건너뛰고 LLM 으로 보냄.
# (도구 1개로 안 풀리는 케이스 신호: "와", "+", "교집합", "함께", ...)
_COMBO_SIGNALS = (
    " 와 ", " 과 ", " + ", "+",
    "그리고", "교집합", "함께", "같이",
    "둘 다", "양쪽",
    "관계", "연관", "상관", "교차",
    "동시", "중에서", "중에 ",
)


def _is_combo_query(q_lower: str) -> bool:
    """복합 질문 신호가 있으면 True — LLM Planner 로 보냄."""
    return any(sig in q_lower for sig in _COMBO_SIGNALS)


def _match_keyword(q_lower: str) -> Optional[str]:
    """단일 도구 키워드 매칭 — 매칭되는 첫 도구 ID 반환, 없으면 None."""
    for tid, kws in _QUICK_KEYWORDS.items():
        for kw in kws:
            if kw in q_lower:
                return tid
    return None


# I-13 (Neo4j 관계 분석) 의 mode 결정 — 발화 키워드 우선순위.
_I13_MODE_KEYWORDS = [
    ("org_chart",        ["조직도", "부서 트리", "조직 도", "부서트리"]),
    ("mutual_eval",      ["상호 평가", "상호평가", "서로 평가"]),
    ("evaluator_path",   ["평가자의 평가자", "평가자의평가자", "2-hop", "2 hop"]),
    ("dept_grade_mix",   ["부서별 등급", "부서별등급", "부서 등급 분포"]),
    ("evaluator_network", ["평가자 네트워크", "관계 네트워크", "평가자네트워크"]),
]


def _infer_i13_mode(q_lower: str) -> str:
    """발화에서 I-13 의 mode 추출 — 매칭 안 되면 evaluator_network 기본."""
    for mode, kws in _I13_MODE_KEYWORDS:
        for kw in kws:
            if kw in q_lower:
                return mode
    return "evaluator_network"


# ─── 데이터 타입 ───
@dataclass
class ToolStep:
    """실행 계획의 한 단계 — 도구 하나 호출."""
    tool: str                                       # indicator_id (예: "I-07")
    params: Dict[str, Any] = field(default_factory=dict)
    depends_on: Optional[str] = None                # 이전 단계 indicator_id
    param_mapping: Dict[str, str] = field(default_factory=dict)
    # param_mapping: 이전 결과에서 어떤 필드를 이 단계 어떤 파라미터로?
    # 예: {"dept_ids": "depts.risky"} → 이전 결과의 depts.risky 를 dept_ids 로


@dataclass
class ToolPlan:
    """전체 실행 계획."""
    steps: List[ToolStep]
    reasoning: str = ""    # LLM 이 왜 이 도구들을 골랐는지 설명 (로깅용)

    def is_single(self) -> bool:
        return len(self.steps) == 1

    def is_empty(self) -> bool:
        return len(self.steps) == 0


# ─── 프롬프트 ───
_PLANNER_PROMPT = """당신은 HR 분석 도구를 선택하는 전문가다.
사용자 질문에 답하기 위해 어떤 분석 도구가 필요한지 결정하라.

# 사용 가능한 도구
{tools_description}

# 사용자 질문
"{query}"

# 지시사항
1. 질문을 분석해서 필요한 도구를 고른다.
2. 단순 질문이면 도구 1개. 복합 질문이면 여러 개.
3. 도구 간 의존성이 있으면 명시한다.
   - 예: "I-07 결과 부서들에 대해 I-02 분석" → I-02 의 depends_on = "I-07"
4. JSON 으로만 답하라. 설명·markdown 금지.

# 응답 형식 (JSON)
{{
  "reasoning": "왜 이 도구들을 골랐는지 한 줄",
  "steps": [
    {{
      "tool": "I-XX",
      "depends_on": null,
      "param_mapping": {{}}
    }}
  ]
}}

# 예시 1 — 단일 도구
질문: "부서별 워라밸 어때"
{{
  "reasoning": "워라밸 단일 분석 요청",
  "steps": [{{"tool": "I-07", "depends_on": null, "param_mapping": {{}}}}]
}}

# 예시 2 — 복합 (병렬)
질문: "워라밸과 보상 누락 같이 봐줘"
{{
  "reasoning": "두 분석을 독립 실행 후 비교",
  "steps": [
    {{"tool": "I-07", "depends_on": null, "param_mapping": {{}}}},
    {{"tool": "I-02", "depends_on": null, "param_mapping": {{}}}}
  ]
}}

# 예시 3 — 의존성 (순차)
질문: "워라밸 위험 부서 중 보상 누락이 있는 사원"
{{
  "reasoning": "I-07 위험 부서를 추려 I-02 에 전달",
  "steps": [
    {{"tool": "I-07", "depends_on": null, "param_mapping": {{}}}},
    {{"tool": "I-02", "depends_on": "I-07", "param_mapping": {{"dept_ids": "depts.risky"}}}}
  ]
}}

JSON 응답:"""


def _build_tools_description() -> str:
    """TOOL_REGISTRY 로부터 도구 설명 텍스트 생성."""
    lines = []
    for tool_id, meta in TOOL_REGISTRY.items():
        title = meta.get("title", "")
        desc = meta.get("description", "")
        lines.append(f"- {tool_id}: {title} — {desc}")
    return "\n".join(lines)


# ─── 메인 함수 ───
def plan_tools(query: str, llm_invoke) -> ToolPlan:
    """
    사용자 발화 → ToolPlan.

    1) 복합 시그널 없는 단순 질문 → 키워드 매칭으로 1개 도구 즉시 선택 (LLM 호출 X)
    2) 복합 시그널 있거나 키워드 매칭 실패 → LLM Planner 호출 (N개 도구 + 의존성)
    3) LLM 도 실패하면 빈 ToolPlan (호출 측에서 폴백)

    Args:
        query: 사용자 발화
        llm_invoke: graph.safe_invoke 같은 LLM 호출 함수 (PII 가드 포함)

    Returns:
        ToolPlan — 실행 단계 리스트.
    """
    q_lower = query.lower()

    # 1차: 빠른 경로 — 복합 시그널 없고 단일 키워드 매칭되면 즉시 반환
    if not _is_combo_query(q_lower):
        matched = _match_keyword(q_lower)
        if matched:
            params: Dict[str, Any] = {}
            # I-13 (Neo4j 관계 분석) — 발화에서 mode 추출
            if matched == "I-13":
                params["mode"] = _infer_i13_mode(q_lower)
            logger.info(f"planner (규칙): '{query[:40]}' → [{matched}] params={params}")
            return ToolPlan(
                steps=[ToolStep(tool=matched, params=params)],
                reasoning="단일 도구 키워드 매칭",
            )

    # 2차: LLM Planner — 복합 의심 또는 키워드 매칭 실패
    tools_desc = _build_tools_description()
    prompt = _PLANNER_PROMPT.format(
        tools_description=tools_desc,
        query=query,
    )

    try:
        response = llm_invoke(prompt, purpose="planner")
        text = response.content if hasattr(response, "content") else str(response)
    except Exception as e:
        logger.exception(f"planner LLM 호출 실패: {e}")
        return ToolPlan(steps=[])

    plan = _parse_llm_response(text)
    if plan.is_empty():
        logger.warning(f"planner: 도구 선택 실패 '{query[:40]}'")
    else:
        tools_picked = [s.tool for s in plan.steps]
        logger.info(f"planner (LLM): '{query[:40]}' → {tools_picked} ({plan.reasoning})")

    return plan


def _parse_llm_response(text: str) -> ToolPlan:
    """LLM 응답에서 JSON 블록 추출 후 ToolPlan 으로 변환."""
    # ```json ... ``` 같은 마크다운 블록 제거
    json_match = re.search(r"\{[\s\S]*\}", text)
    if not json_match:
        logger.warning(f"planner: JSON 블록 못 찾음. response: {text[:200]}")
        return ToolPlan(steps=[])

    try:
        data = json.loads(json_match.group(0))
    except json.JSONDecodeError as e:
        logger.warning(f"planner: JSON 파싱 실패 ({e}). text: {text[:200]}")
        return ToolPlan(steps=[])

    reasoning = data.get("reasoning", "")
    steps_data = data.get("steps", [])
    if not isinstance(steps_data, list):
        return ToolPlan(steps=[])

    steps: List[ToolStep] = []
    for s in steps_data:
        if not isinstance(s, dict):
            continue
        tool = s.get("tool", "").strip()
        if tool not in TOOL_REGISTRY:
            logger.warning(f"planner: 알 수 없는 도구 '{tool}' 무시")
            continue
        steps.append(ToolStep(
            tool=tool,
            params=s.get("params", {}) or {},
            depends_on=s.get("depends_on"),
            param_mapping=s.get("param_mapping", {}) or {},
        ))

    return ToolPlan(steps=steps, reasoning=reasoning)
