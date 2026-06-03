"""
Reasoner — 여러 도구 결과를 종합해서 교차 추론 narrative 생성.

단일 도구 결과는 기존 generate_short 가 처리.
복합 도구(2개 이상) 결과만 reasoner 가 처리해서 다음 효과:
  - 결과 간 공통 부서·사원 식별
  - 두 분석의 관계·상관 추론
  - 우선 검토 대상 추출
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from analysis.tools import TOOL_REGISTRY


logger = logging.getLogger("analysis.agent.reasoner")


_REASONER_PROMPT = """당신은 HR 분석 결과를 종합하는 전문가다.
여러 분석 도구 결과를 비교·교차해서 통찰을 제공하라.

# 사용자 질문
"{query}"

# 분석 결과들
{results_summary}

# 지시사항
1. 각 분석의 핵심 수치를 간단히 정리.
2. 결과 간 공통점 / 차이점 / 관계를 찾아라.
   - 같은 부서·사원이 여러 결과에 등장하는지 (교차 위험)
   - 한 분석의 결과가 다른 분석을 어떻게 보완하는지
3. 우선 검토 대상을 명시 (어느 부서·사원부터 봐야 하나).
4. 8줄 이내로 작성. 불필요한 인사말·반복 금지.
5. markdown 헤더(#) 쓰지 말 것. 순수 텍스트.

답변:"""


def reason_results(
    query: str,
    results: Dict[str, Any],
    errors: Dict[str, str],
    llm_invoke,
) -> str:
    """
    여러 도구 결과 → 종합 추론 narrative.

    Args:
        query: 원래 사용자 발화
        results: {indicator_id: raw_result, ...}
        errors:  {indicator_id: error_msg, ...}
        llm_invoke: graph.safe_invoke 같은 LLM 호출 함수

    Returns:
        narrative 텍스트. LLM 실패 시 폴백 텍스트.
    """
    if not results and not errors:
        return "분석 결과가 없습니다."

    summary_text = _build_results_summary(results, errors)
    prompt = _REASONER_PROMPT.format(query=query, results_summary=summary_text)

    try:
        response = llm_invoke(prompt, purpose="reasoner")
        text = response.content if hasattr(response, "content") else str(response)
        narrative = text.strip()
    except Exception as e:
        logger.exception(f"reasoner LLM 호출 실패: {e}")
        narrative = _fallback_narrative(results, errors)

    tools_used = ", ".join(results.keys()) if results else "없음"
    logger.info(f"reasoner: '{query[:40]}' 종합 완료 (도구={tools_used})")
    return narrative


def _build_results_summary(
    results: Dict[str, Any],
    errors: Dict[str, str],
) -> str:
    """모든 결과를 LLM 입력용으로 압축."""
    parts: List[str] = []

    for tool_id, raw in results.items():
        title = TOOL_REGISTRY.get(tool_id, {}).get("title", "")
        parts.append(f"## [{tool_id}] {title}")
        parts.append(_summarize_one(raw))
        parts.append("")

    for tool_id, err in errors.items():
        parts.append(f"## [{tool_id}] 실행 실패")
        parts.append(f"  에러: {err}")
        parts.append("")

    return "\n".join(parts)


def _summarize_one(raw: Dict[str, Any]) -> str:
    """단일 도구 결과 요약 — 핵심 수치 + 상위 항목."""
    if not raw:
        return "  (결과 없음)"

    lines: List[str] = []

    summary = raw.get("summary", {}) or {}
    if summary.get("mode") == "INSUFFICIENT":
        return f"  표본 부족: {summary.get('skipped_reason', '?')}"

    for k, v in summary.items():
        if isinstance(v, (str, int, float)) and k not in ("skipped_reason", "mode"):
            lines.append(f"  {k}: {v}")

    for key, label in [
        ("candidates", "후보"),
        ("depts", "부서"),
        ("employees", "사원"),
        ("violation_employees", "위반 사원"),
        ("section_3_emp_candidates", "결합 후보"),
    ]:
        items = raw.get(key)
        if isinstance(items, list) and items:
            lines.append(f"  {label}: {len(items)}건")
            samples = _sample_items(items, max_count=5)
            for s in samples:
                lines.append(f"    - {s}")

    return "\n".join(lines) if lines else "  (요약할 정보 없음)"


def _sample_items(items: List[Any], max_count: int = 5) -> List[str]:
    """리스트에서 상위 N개 식별자 추출."""
    samples: List[str] = []
    for item in items[:max_count]:
        if not isinstance(item, dict):
            samples.append(str(item))
            continue
        label = (
            item.get("emp_name")
            or item.get("dept_name")
            or item.get("evaluator_name")
            or item.get("emp_id")
            or item.get("dept_id")
            or "?"
        )
        extra: List[str] = []
        for k in ("labels", "strength", "violation_emp_count", "delta_sa"):
            v = item.get(k)
            if v is not None:
                extra.append(f"{k}={v}")
        if extra:
            samples.append(f"{label} ({', '.join(extra)})")
        else:
            samples.append(str(label))
    return samples


def _fallback_narrative(
    results: Dict[str, Any],
    errors: Dict[str, str],
) -> str:
    """LLM 호출 실패 시 결과 개수만 알리는 기본 텍스트."""
    parts: List[str] = []
    if results:
        for tool_id in results:
            title = TOOL_REGISTRY.get(tool_id, {}).get("title", tool_id)
            parts.append(f"{title} 분석 완료.")
    if errors:
        for tool_id, err in errors.items():
            parts.append(f"{tool_id} 분석 실패: {err}")
    return " ".join(parts) if parts else "결과를 종합하지 못했습니다."
