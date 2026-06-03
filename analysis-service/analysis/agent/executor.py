"""
Executor — ToolPlan 의 도구들을 실제로 실행.

- 의존성 없는 도구들 → ThreadPoolExecutor 로 병렬 실행
- 의존성 있는 도구 → 이전 결과를 파라미터로 받아 순차 실행

기존 graph.execute_tool 과 동일한 도구 호출 방식 사용 (TOOL_REGISTRY[tid]["func"]).
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from analysis.tools import TOOL_REGISTRY
from analysis.agent.planner import ToolPlan, ToolStep


logger = logging.getLogger("analysis.agent.executor")


# ─── 메인 함수 ───
def execute_plan(
    plan: ToolPlan,
    auth_company_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    ToolPlan 의 모든 단계 실행.

    Args:
        plan: planner 가 만든 실행 계획
        auth_company_id: 인증된 회사 UUID — 모든 도구에 자동 주입 (multi-tenant)

    Returns:
        {
            "results": {indicator_id: raw_result, ...},
            "errors":  {indicator_id: error_msg, ...},
            "order":   [indicator_id, ...],   # 실제 실행 순서
        }
    """
    if plan.is_empty():
        return {"results": {}, "errors": {}, "order": []}

    # 1단계: 의존성 분리
    independent: List[ToolStep] = [s for s in plan.steps if not s.depends_on]
    dependent: List[ToolStep] = [s for s in plan.steps if s.depends_on]

    results: Dict[str, Any] = {}
    errors: Dict[str, str] = {}
    order: List[str] = []

    # 2단계: 독립 도구들 → 병렬 실행
    if independent:
        logger.info(f"executor: 병렬 실행 {[s.tool for s in independent]}")
        with ThreadPoolExecutor(max_workers=min(4, len(independent))) as pool:
            future_map = {
                pool.submit(_run_one, step, auth_company_id, {}): step
                for step in independent
            }
            for future in future_map:
                step = future_map[future]
                try:
                    result = future.result()
                    results[step.tool] = result
                except Exception as e:
                    logger.exception(f"executor: '{step.tool}' 실패")
                    errors[step.tool] = str(e)
                order.append(step.tool)

    # 3단계: 의존성 있는 도구들 → 순차 실행 (depends_on 순서대로)
    remaining = list(dependent)
    while remaining:
        progressed = False
        for step in list(remaining):
            # 의존 도구가 이미 실행됐고, 에러 안 났으면 진행 가능
            if step.depends_on in results:
                logger.info(f"executor: 순차 실행 {step.tool} (depends_on={step.depends_on})")
                try:
                    result = _run_one(step, auth_company_id, results)
                    results[step.tool] = result
                except Exception as e:
                    logger.exception(f"executor: '{step.tool}' 실패")
                    errors[step.tool] = str(e)
                order.append(step.tool)
                remaining.remove(step)
                progressed = True
            elif step.depends_on in errors:
                # 의존 도구가 에러 → 이 단계 스킵
                logger.warning(
                    f"executor: '{step.tool}' 스킵 (의존 '{step.depends_on}' 실패)"
                )
                errors[step.tool] = f"의존 도구 '{step.depends_on}' 실패로 스킵"
                order.append(step.tool)
                remaining.remove(step)
                progressed = True
        if not progressed:
            # 무한 루프 방지 — 의존 그래프 끊김
            for step in remaining:
                logger.error(f"executor: '{step.tool}' 의존성 해소 실패 (depends_on={step.depends_on})")
                errors[step.tool] = f"의존성 해소 실패: {step.depends_on}"
                order.append(step.tool)
            break

    return {
        "results": results,
        "errors": errors,
        "order": order,
    }


# ─── 단일 도구 실행 ───
def _run_one(
    step: ToolStep,
    auth_company_id: Optional[str],
    prior_results: Dict[str, Any],
) -> Dict[str, Any]:
    """
    ToolStep 하나 실행. 의존성이 있으면 prior_results 에서 파라미터 추출.
    """
    tool_meta = TOOL_REGISTRY.get(step.tool)
    if not tool_meta:
        raise ValueError(f"알 수 없는 도구: {step.tool}")

    params = dict(step.params or {})

    # 이전 결과에서 파라미터 추출 (param_mapping)
    if step.depends_on and step.param_mapping:
        prior = prior_results.get(step.depends_on)
        if prior:
            extracted = _extract_params(prior, step.param_mapping)
            params.update(extracted)

    # 회사 UUID 자동 주입 (multi-tenant)
    if auth_company_id and "company_id" not in params:
        params["company_id"] = auth_company_id

    logger.info(f"executor: {tool_meta['name']}({_safe_params_repr(params)})")
    return tool_meta["func"](**params)


def _extract_params(prior_result: Dict[str, Any], mapping: Dict[str, str]) -> Dict[str, Any]:
    """
    param_mapping 에 따라 이전 결과에서 파라미터 추출.

    mapping 예: {"dept_ids": "depts.risky"}
      → prior_result["depts"] 중 "위험" 라벨 있는 부서들의 ID 추출

    지원 경로:
      - "key.subkey"        : 단순 nested dict 접근
      - "depts.risky"       : 부서 결과에서 위험 라벨 있는 것만 (특수 처리)
      - "candidates.emp_ids": 후보 사원 ID 추출 (특수 처리)
    """
    extracted: Dict[str, Any] = {}
    for param_name, path in mapping.items():
        try:
            value = _resolve_path(prior_result, path)
            if value is not None:
                extracted[param_name] = value
        except Exception as e:
            logger.warning(f"executor: param_mapping '{path}' 추출 실패 ({e})")
    return extracted


def _resolve_path(result: Dict[str, Any], path: str) -> Any:
    """
    "a.b.c" 같은 경로를 result 에서 추출.
    특수 키 ("risky", "emp_ids" 등)는 추가 가공.
    """
    parts = path.split(".")

    # 특수 케이스 — 자주 쓰는 패턴 하드코딩
    if parts == ["depts", "risky"]:
        depts = result.get("depts", []) or []
        return [
            d.get("dept_id") for d in depts
            if _has_risk_label(d.get("labels", []))
        ]
    if parts == ["candidates", "emp_ids"]:
        candidates = result.get("candidates", []) or []
        return [c.get("emp_id") for c in candidates if c.get("emp_id")]
    if parts == ["violation_employees", "emp_ids"]:
        viols = result.get("violation_employees", []) or []
        return [v.get("emp_id") for v in viols if v.get("emp_id")]

    # 일반 nested dict 접근
    current: Any = result
    for p in parts:
        if isinstance(current, dict):
            current = current.get(p)
        else:
            return None
        if current is None:
            return None
    return current


def _has_risk_label(labels: List[str]) -> bool:
    """라벨 중 위험·법규·편중 키워드 있으면 True."""
    if not labels:
        return False
    risk_keywords = ("위험", "법규", "편중", "과부하", "무급야근")
    return any(any(kw in l for kw in risk_keywords) for l in labels)


def _safe_params_repr(params: Dict[str, Any]) -> str:
    """로깅용 — 큰 리스트는 길이만 표시."""
    parts = []
    for k, v in params.items():
        if isinstance(v, list) and len(v) > 5:
            parts.append(f"{k}=[{len(v)} items]")
        else:
            parts.append(f"{k}={v!r}")
    return ", ".join(parts)
