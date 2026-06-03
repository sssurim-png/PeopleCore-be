"""
LangGraph Custom StateGraph — 분석 AI 메인 그래프 (HITL 보강).

흐름:
  1. classify_intent — analyze / explain / unknown
  2. analyze 분기:
     select_tool → execute_tool → decide_response_form
        ↓ short                        ↓ report
        generate_short → END           build_report (차트+요약)
                                         ↓
                                       gate_generate (HITL #1: "문서 생성?")
                                         ↓ yes      ↓ no
                                       generate_doc → END
                                         ↓
                                       gate_save (HITL #2: "내 문서함 저장?")
                                         ↓ yes      ↓ no
                                       save_to_inbox → END
  3. explain 분기: rag_search → generate_explain_response → END
  4. unknown: fallback → END

HITL 사용법:
  config = {"configurable": {"thread_id": "session_xxx"}}

  # 첫 호출
  state = graph.invoke({...}, config)
  # → interrupt 도달 시 next 노드 정보 반환

  # 사용자 응답 받아 재개
  graph.update_state(config, {"user_decision_generate": "yes"})
  state = graph.invoke(None, config)
"""
from __future__ import annotations

import os
import json
import logging
from datetime import datetime
from functools import lru_cache
from typing import TypedDict, Optional, Dict, Any, List

from langchain_ollama import ChatOllama
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

# SqliteSaver — 운영용 영속 체크포인터. langgraph-checkpoint-sqlite 패키지 필요.
# 없으면 memory 폴백 (개발 환경 호환).
try:
    from langgraph.checkpoint.sqlite import SqliteSaver
    _SQLITE_SAVER_AVAILABLE = True
except ImportError:
    SqliteSaver = None  # type: ignore
    _SQLITE_SAVER_AVAILABLE = False

# OpenAI 클라우드 LLM — 키 있으면 explain·resolve_context 가속, 없으면 폴백
try:
    from langchain_openai import ChatOpenAI
    _OPENAI_AVAILABLE = True
except ImportError:
    ChatOpenAI = None  # type: ignore
    _OPENAI_AVAILABLE = False

from analysis.tools import TOOL_REGISTRY
from analysis.rag.search import search as rag_search
from analysis.report_builder import (
    decide_response_form as decide_form_func,
    build_markdown,
    build_chart_specs,
)
from analysis.hr_client import upload_ai_report
from analysis.prompts import (
    RESOLVE_CONTEXT_PROMPT,
    INTENT_PROMPT,
    TOOL_SELECT_PROMPT,
    SHORT_PROMPT,
    EXPLAIN_PROMPT,
    EXPLAIN_PROMPT_DETAILED,
    GRADE_CHUNKS_PROMPT,
    GRADE_ANSWER_PROMPT,
    REWRITE_QUERY_PROMPT,
)


logger = logging.getLogger("analysis.graph")


# ─── 상태 정의 ───
class AnalysisState(TypedDict, total=False):
    # 입력
    user_query: str                      # 그래프 내부 사용 — resolve_context 가 재작성 가능
    original_query: Optional[str]        # 사용자 원문 (재작성 전)
    auth_company_id: Optional[str]       # 헤더 X-User-Company
    auth_user_emp_id: Optional[int]      # 헤더 X-User-Id

    # 멀티턴 대화 — 같은 thread_id 내 누적
    conversation_history: List[Dict[str, Any]]   # [{user, indicator_id, summary}, ...]
    context_mode: Optional[str]                  # "fresh" / "continue" / "reset"

    # 의도·도구
    intent: str                          # analyze / explain / unknown
    tool_id: Optional[str]
    tool_params: Dict[str, Any]
    tool_result: Optional[Dict[str, Any]]

    # Multi-tool Agent — Planner / Executor / Reasoner 추가
    tool_plan: Optional[Any]                       # planner.ToolPlan
    tool_results_all: Optional[Dict[str, Any]]     # {indicator_id: raw_result, ...}
    tool_errors_all: Optional[Dict[str, str]]      # {indicator_id: error_msg, ...}
    reasoner_narrative: Optional[str]              # 복합 결과 종합 narrative (있으면 generate_short 우회)

    # RAG (explain) + CRAG 자기수정 루프
    rag_context: Optional[List[Dict[str, Any]]]
    rewrite_count: int                     # explain rewrite 시도 횟수 (max _MAX_REWRITES)
    chunks_grade: Optional[str]            # "relevant" / "irrelevant"
    answer_grade: Optional[str]            # "good" / "bad"

    # 응답 형식
    response_form: Optional[str]         # short / report
    chart_specs: Optional[List[Dict[str, Any]]]
    markdown_report: Optional[str]       # generate_doc 후 채워짐 (save_to_inbox 내부용)

    # HITL 결정
    user_decision_generate: Optional[str]  # yes / no
    user_decision_save: Optional[str]      # yes / no
    user_decision_detail: Optional[str]    # explain HITL — "더 자세히?" 결정 (yes/no)
    user_decision_navigate: Optional[str]  # navigate HITL — "페이지 이동?" 결정 (yes/no)
    is_brief_explain: Optional[bool]       # BRIEF EXPLAIN 후 자세한 답 제안 가능 여부 표시

    # navigate 분기 — 매칭된 대상 페이지 메타
    target_page: Optional[Dict[str, Any]]  # {"key", "url", "label"}

    # 저장 결과 (filevault FileItem id / 부모 폴더 메타)
    saved_report_id: Optional[int]
    saved_folder_id: Optional[int]
    saved_folder_name: Optional[str]
    save_error: Optional[str]

    # 최종 응답
    response: Dict[str, Any]
    error: Optional[str]


# ─── LLM 싱글톤 ───
# 두 종류:
#   _llm        : 로컬 EXAONE — 민감 데이터 분석(analyze) 흐름. 외부 API egress 차단.
#   _cloud_llm  : OpenAI gpt-4o-mini — explain · resolve_context 등 비민감 빠른 응답용.
#                 OPENAI_API_KEY 미설정·langchain-openai 미설치 시 None → 호출부가 EXAONE 폴백.
_llm: Optional[ChatOllama] = None
_cloud_llm: Optional[Any] = None
_cloud_llm_init_tried: bool = False


def get_llm() -> ChatOllama:
    global _llm
    if _llm is None:
        _llm = ChatOllama(
            model=os.getenv("LLM_MODEL", "exaone3.5:7.8b"),
            base_url=os.getenv("OLLAMA_URL", "http://localhost:11434"),
            temperature=0.2,
            num_ctx=4096,
        )
    return _llm


def get_cloud_llm() -> Optional[Any]:
    """OpenAI gpt-4o-mini 인스턴스 반환. 키·라이브러리 없으면 None.

    호출부는 None 이면 get_llm() 폴백.
    직접 호출하지 말고 safe_invoke() 통해 PII 가드 거치기.
    """
    global _cloud_llm, _cloud_llm_init_tried
    if _cloud_llm is not None or _cloud_llm_init_tried:
        return _cloud_llm
    _cloud_llm_init_tried = True

    if not _OPENAI_AVAILABLE:
        logger.info("cloud LLM: langchain-openai 미설치 → EXAONE 폴백")
        return None
    if not os.getenv("OPENAI_API_KEY"):
        logger.info("cloud LLM: OPENAI_API_KEY 미설정 → EXAONE 폴백")
        return None

    try:
        _cloud_llm = ChatOpenAI(
            model=os.getenv("CLOUD_LLM_MODEL", "gpt-4o-mini"),
            temperature=0.2,
            timeout=20,
        )
        logger.info(f"cloud LLM 활성: {os.getenv('CLOUD_LLM_MODEL', 'gpt-4o-mini')}")
    except Exception as e:
        logger.warning(f"cloud LLM 초기화 실패 → EXAONE 폴백: {e}")
        _cloud_llm = None
    return _cloud_llm


# ─── PII 가드 ───
# 사원 정보가 cloud LLM(외부 OpenAI)으로 흘러나가지 않도록 prompt 내용 검문.
# 정책: 매칭 시 cloud 차단 + EXAONE 로컬 폴백 + WARNING 로그.
#
# 패턴은 "데이터 값" 만 잡도록 — 컬럼명만 등장 (RAG 문서가 emp_name 을 설명) 은 미차단.
import re as _re_pii

_PII_PATTERNS = [
    # 사번·식별자 = 값
    _re_pii.compile(r"emp_id\s*[=:]\s*\d+", _re_pii.IGNORECASE),
    _re_pii.compile(r"evaluator_id\s*[=:]\s*\d+", _re_pii.IGNORECASE),
    _re_pii.compile(r"evaluatee_emp_id\s*[=:]\s*\d+", _re_pii.IGNORECASE),
    _re_pii.compile(r"dept_id\s*[=:]\s*\d+", _re_pii.IGNORECASE),
    # 한국어 사번 표기
    _re_pii.compile(r"사번\s*[=:]?\s*\d+"),
    # 주민번호
    _re_pii.compile(r"\b\d{6}-[1-4]\d{6}\b"),
    # 이메일
    _re_pii.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
    # 휴대폰
    _re_pii.compile(r"\b01[016789]-?\d{3,4}-?\d{4}\b"),
]


def _detect_pii(text: str) -> List[str]:
    """텍스트에서 PII 패턴 매칭 결과 반환. 빈 리스트 = 안전."""
    if not text:
        return []
    found: List[str] = []
    for pat in _PII_PATTERNS:
        m = pat.search(text)
        if m:
            found.append(f"{pat.pattern} (예: '{m.group(0)[:40]}')")
    return found


def safe_invoke(prompt: str, *, purpose: str = "unknown") -> Any:
    """cloud LLM 호출 가드 — PII 감지 시 cloud 차단 + EXAONE 로컬 폴백.

    Args:
        prompt: LLM 에 보낼 prompt 전체 텍스트
        purpose: 로깅용 호출 노드 식별자 (예: "resolve_context", "explain")

    Returns:
        LLM response (cloud 또는 local)
    """
    cloud = get_cloud_llm()
    if cloud is None:
        return get_llm().invoke(prompt)

    found = _detect_pii(prompt)
    if found:
        logger.warning(
            f"PII guard [{purpose}]: 사원 정보 감지 → cloud 차단, EXAONE 폴백. "
            f"매칭: {found[:3]}"
        )
        return get_llm().invoke(prompt)

    return cloud.invoke(prompt)


# ─── 노드 0: 컨텍스트 해소 (멀티턴 대화) ───
#
# 같은 thread_id 안에서 이전 발화 이력을 보고:
#   - "그 사원" 같은 지시어가 있으면 → 직전 컨텍스트 슬롯으로 치환해 self-contained 발화로 재작성
#   - 새 주제로 전환된 발화면 → 이력 초기화 (fresh start)
#
# LLM 가용성·품질이 낮을 수 있으니 규칙 기반 휴리스틱을 먼저 적용 — 명백한 새 도구 키워드가
# 등장하면 LLM 안 부르고 즉시 reset 처리.

# 멀티턴 이력 보존 한계 — 이 이상은 가장 오래된 턴부터 폐기
_HISTORY_MAX_TURNS = 10

# 직전 발화를 가리키는 지시어 (있으면 'continue' 후보)
_CONTINUATION_MARKERS = [
    "그 사원", "그사원", "그분", "이 사원", "이사원",
    "해당 사원", "위 사원", "그 부서", "이 부서", "해당 부서", "위 부서",
    "그건", "그것", "그거", "이건", "이것", "이거",
    "더 자세히", "더자세히", "더 보여", "추가로",
    "다른 정보", "다른정보", "다른 거", "또", "그리고",
]


# RESOLVE_CONTEXT_PROMPT → analysis/prompts/context.py


def resolve_context(state: AnalysisState) -> Dict[str, Any]:
    query = state["user_query"]
    history = state.get("conversation_history") or []

    # 첫 발화 — 컨텍스트 없음
    if not history:
        logger.info("컨텍스트: fresh (이력 없음)")
        return {
            "context_mode": "fresh",
            "original_query": query,
        }

    q_lower = query.lower()

    # 규칙 1: 지시어 없고 도구 키워드 명확 → 즉시 reset (LLM 안 부름)
    has_continuation = any(m in q_lower for m in _CONTINUATION_MARKERS)
    has_strong_tool_kw = any(
        kw in q_lower
        for kws in _TOOL_KEYWORDS.values()
        for kw in kws
    )
    if not has_continuation and has_strong_tool_kw:
        logger.info(f"컨텍스트: reset (도구 키워드 명확) — '{query[:30]}...'")
        return {
            "context_mode": "reset",
            "original_query": query,
            "conversation_history": [],
        }

    # 규칙 2: 지시어 없고 explain 의문/명사 명확 → fresh 즉시 통과 (LLM 안 부름)
    # "Z-score 알려줘", "강제분포 뭐야" 같은 단발성 정의 질문은 컨텍스트 해소 불필요.
    has_explain_marker = any(k in q_lower for k in _EXPLAIN_KEYWORDS)
    if not has_continuation and has_explain_marker:
        logger.info(f"컨텍스트: fresh (explain 단발 질문) — '{query[:30]}...'")
        return {
            "context_mode": "fresh",
            "original_query": query,
        }

    # LLM 호출 — 직전 3턴만 (토큰 절약)
    recent = history[-3:]
    history_str = "\n".join(
        f"[{i+1}] 사용자: \"{h.get('user', '')}\" → "
        f"{h.get('indicator_id', '?')} / {h.get('summary', '')}"
        for i, h in enumerate(recent)
    )

    try:
        # 비민감 라우팅 — cloud LLM 우선, 단 PII 감지 시 자동으로 EXAONE 폴백
        prompt = RESOLVE_CONTEXT_PROMPT.format(history=history_str, query=query)
        response = safe_invoke(prompt, purpose="resolve_context")
        text = response.content.strip()
    except Exception as e:
        logger.warning(f"resolve_context LLM 실패 → fresh 폴백: {e}")
        return {
            "context_mode": "fresh",
            "original_query": query,
        }

    # 응답 파싱
    related = False
    rewritten = query
    for line in text.splitlines():
        line = line.strip()
        if line.upper().startswith("RELATED:"):
            related = "yes" in line.split(":", 1)[1].lower()
        elif line.upper().startswith("QUERY:"):
            rewritten = line.split(":", 1)[1].strip().strip('"').strip("'")
            if not rewritten:
                rewritten = query

    if related:
        logger.info(f"컨텍스트: continue — '{query}' → '{rewritten}'")
        return {
            "context_mode": "continue",
            "original_query": query,
            "user_query": rewritten,
        }
    logger.info(f"컨텍스트: reset — '{query[:30]}...'")
    return {
        "context_mode": "reset",
        "original_query": query,
        "conversation_history": [],
    }


# ─── 노드 1: Intent 분류 (규칙 우선 + LLM fallback) ───

# 키워드 룰 — 작은 모델 신뢰도 부족 보완
# 원칙: analyze 는 명령형(동사) 위주, explain 은 의문형 위주

_EXPLAIN_KEYWORDS = [
    # 의문 표현
    "뭐야", "뭔가", "뭔데", "뭐예요", "뭐임", "뭔뜻", "뭣", "무엇",
    "어떻게", "어떤", "어디서", "어디", "언제", "왜",
    # 명사형 설명 요청
    "기준", "정의", "설명", "뜻", "의미", "방법", "방식", "흐름", "절차",
    "단계", "동작", "처리", "알려줘", "알려주", "가이드",
]

# 분석은 명령형 동사 위주 (모호한 명사는 제외)
_ANALYZE_KEYWORDS = [
    # 명령형 (가장 강한 시그널)
    "보여줘", "보여 줘", "찾아", "도출", "발굴", "분석해", "진단해",
    "리포트", "보고서", "통합", "종합",
    # 명확한 분석 요청
    "분석 좀", "진단 좀", "검토",
    # 발굴 대상 (명령형과 결합 시 강력)
    "후보 도출", "후보 발굴", "위반 부서", "누락 사원",
]


# navigate 시그널 — "어디서/어떻게 X 하지" 류 페이지 안내 의도
_NAVIGATE_QUESTION = ["어떻게"]                              # 방법 의문어 (어디 류는 별도 처리)
_NAVIGATE_PLACE = ["메뉴", "페이지", "화면"]                  # 장소 명사
_NAVIGATE_ACTION = [                                          # 행동 동사
    "진행", "시작", "작성", "신청", "제출", "이동", "들어가",
    "조정", "설정", "변경", "수정", "관리", "등록", "확인", "조회",
]
_NAVIGATE_EOMI = ["려면", "하려면"]                          # 어미 (~려면 / ~하려면)


# 페이지 레지스트리 — keywords 매칭으로 target 페이지 식별.
# url / hint 는 PeopleCore FE 라우트(be23-fin-2team-PeopleCore-fe)에 맞춤.
# hint: 페이지 안에서 deep-link 안 되는 탭/섹션 안내가 필요할 때 narrative 보강.
_PAGE_REGISTRY: Dict[str, Dict[str, Any]] = {
    # 결과 조회 — "결과 어디서 보냐" 류 발화. dict 순서 우선 매칭 위해 맨 앞에 둠.
    "eval_result_view": {
        "keywords": [
            "평가 결과", "평가결과",
            "성과 결과", "성과결과",
            "결과 조회", "결과 확인", "결과 보기", "결과 보고",
            "결과를 보", "결과 봐", "결과 어디", "결과를 어디",
            "결과 화면", "결과 페이지",
        ],
        "url": "/eval-admin?tab=result-view",
        "label": "평가 결과 조회",
        "hint": "전사 평가 결과를 조회할 수 있습니다.",
    },
    "performance_evaluation": {
        "keywords": [
            "성과평가", "성과 평가", "성과", "고과",
            "평가 진행", "평가 작성", "평가 시작",
            "자기평가", "자기 평가", "목표 등록", "목표등록",
        ],
        "url": "/eval",
        "label": "성과평가",
    },
    "ai_report": {
        "keywords": [
            "ai 리포트", "ai리포트", "ai 보고서", "ai보고서",
            "분석 결과", "분석 리포트", "분석 보고서", "분석 보기",
            "리포트 결과", "보고서 결과",
            "결과 확인", "결과 조회", "결과를 보", "결과 보",
        ],
        "url": "/hr/report",
        "label": "AI 리포트",
    },
    "eval_admin_rules": {
        "keywords": [
            "분석 규칙", "평가 규칙", "규칙 관리", "규칙",
            "임계값", "기준값", "등급 규칙",
            "zscore", "z-score", "z score", "z스코어",
            "평가 관리",
        ],
        "url": "/eval-admin?tab=rules",
        "label": "평가 규칙 관리",
        "hint": "임계값·등급 규칙을 조정할 수 있습니다.",
    },
}


def _matches_navigate(query: str, q_lower: str) -> bool:
    """navigate 시그널 + 페이지 매칭 둘 다 성립할 때만 True.

    페이지 매칭 안 되면 False → explain 으로 빠지게 둠 (RAG 가 답변 시도).
    """
    # 1) navigate 시그널
    nav_signal = (
        "어디" in q_lower
        or any(k in q_lower for k in _NAVIGATE_PLACE)
        or any(s in q_lower for s in _NAVIGATE_EOMI)
        or (any(k in q_lower for k in _NAVIGATE_QUESTION)
            and any(k in q_lower for k in _NAVIGATE_ACTION))
    )
    if not nav_signal:
        return False

    # 2) 페이지 매칭 — 한 개라도 매칭되어야 navigate 인정
    for meta in _PAGE_REGISTRY.values():
        if any(kw.lower() in q_lower for kw in meta["keywords"]):
            return True
    return False


# INTENT_PROMPT → analysis/prompts/intent.py


def _is_question(query: str) -> bool:
    """발화가 의문문인지 판단 (explain 시그널 보강)."""
    q = query.strip()
    if q.endswith("?") or q.endswith("？"):
        return True
    # 한국어 의문형 어미
    return any(q.endswith(suffix) for suffix in [
        "야", "지", "나요", "가요", "에요", "예요", "까요", "을까", "을까요",
        "지요", "는가", "는지", "있나", "없나",
    ])


def classify_intent(state: AnalysisState) -> Dict[str, Any]:
    query = state["user_query"]
    q_lower = query.lower()

    # 1차: 규칙 기반 (즉시, LLM 호출 X)
    has_explain = any(k in q_lower for k in _EXPLAIN_KEYWORDS)
    has_analyze = any(k in q_lower for k in _ANALYZE_KEYWORDS)
    has_tool_kw = any(kw in q_lower for kws in _TOOL_KEYWORDS.values() for kw in kws)
    is_q = _is_question(query)

    # 강한 명령형 analyze: 우선
    strong_analyze_cmds = ["보여줘", "보여 줘", "찾아", "분석해", "진단해", "도출", "발굴"]
    has_strong_analyze = any(k in q_lower for k in strong_analyze_cmds)

    # 강한 explain: 의문문 + explain 명사 (예: "산업안전 기준이 뭐야")
    # 이건 도구 키워드가 있어도 explain 우선.
    explain_nouns = ["기준", "정의", "뜻", "의미", "설명"]
    has_strong_explain_q = is_q and any(n in q_lower for n in explain_nouns)

    # CRAG 상태는 매 턴 리셋 (이전 턴의 rewrite_count·grade 가 새 explain 루프에 누수되지 않게)
    crag_reset = {"rewrite_count": 0, "chunks_grade": None, "answer_grade": None}

    if has_strong_analyze:
        logger.info(f"Intent (규칙·강한 analyze): '{query[:30]}...' → analyze")
        return {"intent": "analyze", **crag_reset}
    if has_strong_explain_q:
        logger.info(f"Intent (규칙·explain 의문): '{query[:30]}...' → explain")
        return {"intent": "explain", **crag_reset}
    # navigate — 위치/방법 묻기 + 페이지 키워드 매칭. tool_kw 보다 먼저 체크
    # (예: "성과평가 어디서 해" 가 평가자 분석으로 빠지지 않게)
    if _matches_navigate(query, q_lower):
        logger.info(f"Intent (규칙·navigate): '{query[:30]}...' → navigate")
        return {"intent": "navigate", **crag_reset}
    # 도구 키워드(산업안전·워라밸·평가자 등)는 analyze 도메인 → analyze 강제
    if has_tool_kw or has_analyze:
        logger.info(f"Intent (규칙·analyze): '{query[:30]}...' → analyze")
        return {"intent": "analyze", **crag_reset}
    if has_explain or is_q:
        logger.info(f"Intent (규칙·explain): '{query[:30]}...' → explain")
        return {"intent": "explain", **crag_reset}

    # 2차: LLM fallback (모호한 발화)
    llm = get_llm()
    response = llm.invoke(INTENT_PROMPT.format(query=query))
    text = response.content.strip().lower()

    # navigate 우선 체크 (analyze/explain 과 substring 충돌 없음)
    if "navigate" in text:
        intent = "navigate"
    elif "analyze" in text:
        intent = "analyze"
    elif "explain" in text:
        intent = "explain"
    else:
        # 마지막 fallback: analyze (가장 흔한 의도)
        intent = "analyze"

    logger.info(f"Intent (LLM): '{query[:30]}...' → {intent}")
    return {"intent": intent, **crag_reset}


# ─── 노드 2: 도구 선택 (규칙 우선 + LLM fallback) ───

# 도구별 키워드 매핑
# 도구 키워드 — TOOL_REGISTRY 의 keywords 필드를 single source of truth 로 참조.
# 도구 추가·키워드 변경 시 tools/__init__.py 한 곳만 수정하면 graph + planner 모두 반영됨.
from analysis.tools import get_tool_keywords
_TOOL_KEYWORDS = get_tool_keywords()


# TOOL_SELECT_PROMPT → analysis/prompts/intent.py


# ─── Multi-tool Agent — Planner / Executor / Reasoner ───
# 기존 select_tool + execute_tool 을 대체.
# Planner 가 도구 N개를 선택하고, Executor 가 병렬·순차 실행한 뒤,
# 결과가 2개 이상이면 Reasoner 가 종합 narrative 를 생성한다.
from analysis.agent.planner import plan_tools
from analysis.agent.executor import execute_plan
from analysis.agent.reasoner import reason_results


def planner_node(state: AnalysisState) -> Dict[str, Any]:
    """사용자 발화 → ToolPlan (도구 N개 + 의존성).
    단순 질문은 키워드 매칭으로 즉시 결정 (LLM 우회).
    """
    query = state["user_query"]
    plan = plan_tools(query, safe_invoke)
    if plan.is_empty():
        # 폴백: 종합 진단으로
        logger.warning(f"planner: 빈 plan, I-12 fallback ('{query[:30]}')")
        from analysis.agent.planner import ToolPlan, ToolStep
        plan = ToolPlan(steps=[ToolStep(tool="I-12")], reasoning="fallback")
    return {"tool_plan": plan}


def executor_node(state: AnalysisState) -> Dict[str, Any]:
    """ToolPlan 의 도구들을 실행 (독립=병렬, 의존=순차)."""
    plan = state.get("tool_plan")
    if plan is None or plan.is_empty():
        return {"error": "실행할 도구 계획이 없습니다."}

    output = execute_plan(plan, auth_company_id=state.get("auth_company_id"))
    results = output["results"]
    errors = output["errors"]

    update: Dict[str, Any] = {
        "tool_results_all": results,
        "tool_errors_all": errors,
    }

    # 단일 도구 → 기존 키 호환 (generate_short / decide_response_form 그대로 사용)
    if len(results) == 1:
        tid = next(iter(results))
        update["tool_id"] = tid
        update["tool_result"] = results[tid]
        return update

    # 결과 0개 + 에러 1개 이상 → 단일 실패로 처리
    if not results and errors:
        tid = next(iter(errors))
        update["error"] = f"도구 실행 오류: {errors[tid]}"
        update["tool_id"] = tid
        return update

    # 복합 → tool_id 는 종합 의미로 'I-12' (후속 흐름 호환), tool_result 는 첫 번째
    # reasoner_node 가 narrative 를 만들어 state["reasoner_narrative"] 에 저장.
    first_tid = next(iter(results))
    update["tool_id"] = "I-12"
    update["tool_result"] = results[first_tid]
    return update


def reasoner_node(state: AnalysisState) -> Dict[str, Any]:
    """복합 결과 종합 narrative — 단일 케이스는 호출되지 않는다 (라우터가 우회)."""
    query = state.get("original_query") or state.get("user_query", "")
    results = state.get("tool_results_all") or {}
    errors = state.get("tool_errors_all") or {}

    narrative = reason_results(query, results, errors, safe_invoke)
    return {"reasoner_narrative": narrative}


# ─── 라우터: executor 후 분기 ───
def route_after_executor(state: AnalysisState) -> str:
    """결과가 2개 이상이면 reasoner 거치고, 아니면 바로 generate_short 로."""
    if state.get("error"):
        return "generate_short"
    results = state.get("tool_results_all") or {}
    return "reasoner" if len(results) >= 2 else "generate_short"


# ─── 노드 4: 응답 형식 결정 (short / report) ───
def decide_response_form(state: AnalysisState) -> Dict[str, Any]:
    if state.get("error"):
        return {"response_form": "short"}  # 에러는 짧게
    tool_id = state.get("tool_id")
    result = state.get("tool_result") or {}
    form = decide_form_func(tool_id, result)
    logger.info(f"응답 형식: {form} (tool={tool_id})")
    return {"response_form": form}


# ─── 분석 결과 요약 — LLM 토큰 절약 ───
# LLM·UI 노출 시 사용자가 이해할 수 있도록 도구 내부 코드값을 한국어로 변환.
_MODE_LABELS = {
    "FULL":         "정상 분석 (표본 충분)",
    "ZSCORE":       "Z-score 분석 (표본 중간)",
    "PARTIAL":      "부분 분석 (표본 중간)",
    "FALLBACK":     "참고용 (표본 부족 — 상위·하위만 도출)",
    "INSUFFICIENT": "표본 미달 (분석 생략)",
}


# summary 키 한국어 라벨 — 도구 영문 키를 사용자가 읽는 표현으로.
_SUMMARY_LABELS = {
    "mode":                    "평가 모드",
    "n_evaluators":            "평가자 수",
    "n_evaluatees":            "피평가자 수",
    "n_candidates":            "후보 사원 수",
    "n_employees":             "사원 수",
    "n_depts":                 "부서 수",
    "total_violation_emps":    "법규 위반 사원",
    "underpaid_count":         "보상 누락 후보",
    "unfair_count":            "부당 보상 후보",
    "season_id":               "시즌 ID",
    "season":                  "시즌",
    "company_avg_score":       "회사 평균 점수",
    "company_avg_sa_score":    "회사 평균 SA 점수",
    "company_sa_ratio":        "회사 SA 비율",
    "total_score":             "총점",
    "min_pool_size":           "최소 풀 크기",
    "analysis_period_months":  "분석 기간(개월)",
    "weekly_hour_limit":       "주간 한도(시간)",
    "max_consecutive_work_days": "최대 연속 근무일",
}


def _humanize_value(key: str, value: Any) -> str:
    """summary 값 중 코드 형태(FALLBACK 같은)는 한국어 라벨로 치환."""
    if key == "mode" and isinstance(value, str) and value in _MODE_LABELS:
        return _MODE_LABELS[value]
    return str(value)


def _humanize_key(key: str) -> str:
    """summary 키를 한국어 라벨로. 매핑 없으면 원본 그대로."""
    return _SUMMARY_LABELS.get(key, key)


def _summarize_result_for_llm(result: Dict[str, Any]) -> str:
    """raw_result 의 핵심만 추출해 짧은 텍스트로 — LLM 컨텍스트 절약.
    도구 내부 코드값(FALLBACK 등)은 사용자가 이해하는 표현으로 치환해 전달한다."""
    if not result:
        return "결과 없음"

    parts = []
    indicator = result.get("indicator_id", "?")
    title = result.get("report_id", "")
    parts.append(f"분석: {indicator} {title}")

    summary = result.get("summary", {})
    if summary.get("mode") == "INSUFFICIENT":
        return f"{parts[0]}\n{_MODE_LABELS['INSUFFICIENT']}: {summary.get('skipped_reason', '?')}"

    # summary 의 주요 수치 — 키와 값 모두 한국어로 치환
    for k, v in summary.items():
        if isinstance(v, (str, int, float)) and k != "skipped_reason":
            parts.append(f"  {_humanize_key(k)}: {_humanize_value(k, v)}")

    # I-13 Neo4j 결과 — rows 필드는 표 형태로 직접 풀어주기
    rows = result.get("rows")
    if isinstance(rows, list) and rows:
        parts.append(f"  결과 {len(rows)}건:")
        for row in rows[:10]:
            if isinstance(row, dict):
                parts.append("    - " + ", ".join(f"{k}={v}" for k, v in row.items()))

    # candidates / depts / employees 수만 간단히
    for key, label in [("candidates", "후보 사원"), ("depts", "부서"),
                       ("employees", "사원"), ("violation_employees", "위반 사원"),
                       ("section_3_emp_candidates", "결합 후보")]:
        items = result.get(key)
        if isinstance(items, list) and items:
            parts.append(f"  {label}: {len(items)}건")
            # 상위 3개만 핵심 정보
            for item in items[:3]:
                if isinstance(item, dict):
                    name = item.get("emp_name") or item.get("dept_name") or item.get("evaluator_name") or "?"
                    extra_keys = ["pattern", "label", "case", "strength", "labels"]
                    extra = next((f" [{item[k]}]" for k in extra_keys if k in item), "")
                    parts.append(f"    - {name}{extra}")

    return "\n".join(parts)


# ─── 노드 5a: 분석 narrative (글 위주, 데이터 양에 맞춰 길이 조절) ───
#
# 작성 원칙:
# - 작은 모델(2.4B)은 prompt 안의 단어를 그대로 echo 하는 경향이 있어
#   "금지어 리스트" 식 negative-priming 은 역효과 (오히려 그 단어를 사용함).
# - 따라서 prompt 에는 **시스템에 없는 지표 단어 자체를 한 글자도 두지 않는다.**
# - 오직 positive 예시만 보여주고 모델이 패턴을 흉내 내게 한다.
# SHORT_PROMPT → analysis/prompts/analyze.py


# 명시적 보고서 요청 — 발화에 있으면 무조건 제안
_REPORT_REQUEST_KEYWORDS = [
    "보고서", "리포트", "report",
    "종합", "통합", "전사 진단", "전체 진단",
    "정리", "정리해", "문서로",
]

# 분석·진단 의도를 시사하는 동사·명사 — 명단·표·차트 결과를 기대하는 발화
_ANALYTICAL_INTENT_KEYWORDS = [
    "분석", "진단", "발굴", "도출", "찾아", "보여줘", "보여 줘",
    "명단", "후보", "비교", "점검", "검토",
    "분포", "패턴", "변동", "편향", "누락",
    "위반", "위험",
]


def _should_offer_report(user_query: str, result: Dict[str, Any]) -> bool:
    """보고서 생성 게이트 노출 여부.

    표본이 부족하면 무조건 X. 그 외엔:
      1) 명시적 보고서 요청 → 제안
      2) 분석·진단 의도 동사·명사 → 제안 (분석 목적의 발화)
      3) 결과 명단 합계 ≥ 5 → 제안 (시각화 가치 있는 분량)
      4) 그 외 단발성 정보 질의 → 제안 X
    """
    summary = result.get("summary") or {}
    if summary.get("mode") == "INSUFFICIENT":
        return False

    q = user_query.lower()
    if any(k in q for k in _REPORT_REQUEST_KEYWORDS):
        return True
    if any(k in q for k in _ANALYTICAL_INTENT_KEYWORDS):
        return True

    rows = (
        len(result.get("candidates") or [])
        + len(result.get("section_3_emp_candidates") or [])
        + len(result.get("depts") or [])
        + len(result.get("employees") or [])
        + len(result.get("violation_employees") or [])
    )
    return rows >= 5


# ─── 객관적 narrative 빌더 (LLM 우회) ───
# 작은 모델이 어떤 프롬프트에도 추측·해석을 끼워넣어서, 사실 나열은 결정적 코드로만 처리.

# summary 키 → 한국어 라벨 (객관 narrative 표시용)
_SUMMARY_LABEL_KR = {
    "n_evaluators": "평가자 수",
    "n_evaluatees": "피평가자 수",
    "n_candidates": "후보 수",
    "total_violation_emps": "법규 위반 사원",
    "underpaid_count": "보상 누락 후보",
    "unfair_count": "부당 보상 후보",
    "season_id": "시즌 ID",
    "company_sa_ratio": "전사 S+A 비율",
    "biased_dept_count": "편중 부서 수",
    "mode": "모드",
    "n_depts": "부서 수",
    "n_employees": "사원 수",
}

# 행 필드 키 → 한국어 라벨. 모든 도구에서 등장하는 필드를 모아 둠.
# 매핑에 없는 키는 영문 그대로 노출 (라벨 없이도 의미 추정 가능한 것은 둠).
_FIELD_LABEL_KR = {
    # 공통 식별
    "emp_id": "사원 ID",
    "dept_id": "부서 ID",
    "emp_name": "사원명",
    "dept_name": "부서명",
    "evaluator_name": "평가자명",
    # 등급·평가
    "grade_name": "등급",
    "grades_seq": "등급 이력",
    "n_seasons": "시즌 수",
    "pattern": "패턴",
    "label": "라벨",
    "labels": "라벨",
    "case": "유형",
    "strength": "강도",
    "signals": "시그널",
    "flag_reasons": "지표",
    # 분포·비율
    "n": "인원",
    "dept_n": "부서 인원",
    "s_ratio": "S 비율",
    "a_ratio": "A 비율",
    "sa_ratio": "S+A 비율",
    "delta_sa": "전사 평균과 차이",
    "label_grade_dist": "등급 분포 라벨",
    # 보상·연봉
    "tenure_years": "재직 연차",
    "years_since_last_promotion": "마지막 진급 후 연차",
    "season_pay": "시즌 보상",
    "salary_pctile": "직급 내 연봉 백분위",
    "corr_grade_bonus": "등급-성과급 상관",
    "label_short_term": "단기 변별력 라벨",
    # 평가자 분포
    "n_evaluatees": "피평가자 수",
    "mean_score": "평균 점수",
    "stdev_score": "표준편차",
    "mean_z": "Z점수 평균",
    # 워라밸
    "mean_overtime_per_emp": "1인당 야근(분/월)",
    "mean_night_per_emp": "1인당 야간(분/월)",
    "holiday_work_ratio": "휴일근무 비율",
    "unrecognized_ot_ratio": "미인정 초과 비율",
    "label_workload": "워라밸 라벨",
    # 산업안전
    "violation_types": "위반 유형",
    "max_weekly_hours": "최대 주간 근무",
    "max_consecutive_days": "최대 연속 근무일",
    "violation_emp_count": "위반 사원 수",
    "consec_violation_emp_count": "연속근무 위반 사원 수",
    "label_safety": "안전 라벨",
}


def _ko_field(key: str) -> str:
    return _FIELD_LABEL_KR.get(key, key)

# 명단성 리스트 → 한국어 라벨
_LIST_LABEL_KR = [
    ("candidates", "후보 사원"),
    ("section_3_emp_candidates", "결합 후보 사원"),
    ("depts", "부서"),
    ("employees", "사원"),
    ("violation_employees", "위반 사원"),
]


# 행 표시 시 "이름" 으로 쓸 우선순위 키
_ROW_NAME_KEYS = ("emp_name", "dept_name", "evaluator_name", "name")
# 행 표시 시 식별자로 쓸 폴백 키 (이름이 없을 때)
_ROW_ID_KEYS = ("emp_id", "dept_id", "evaluator_id", "id")
# 행 한 줄에 표시할 최대 필드 수 (너무 길면 가독성 저하)
_ROW_MAX_FIELDS = 8

# 명단별 narrative 노출 캡 — 리스트가 많아도 본문이 100줄 이상으로 폭주하지 않게.
# 풀 데이터는 '보고서 생성' 단계에서 마크다운·표로 노출됨.
_LIST_ROW_CAP = 25


def _render_row_inline(row: Dict[str, Any]) -> str:
    """단일 행 → 한 줄. 모든 primitive 필드 노출."""
    name: Optional[str] = None
    for k in _ROW_NAME_KEYS:
        v = row.get(k)
        if v not in (None, ""):
            name = str(v)
            break
    fallback_id_key: Optional[str] = None
    if not name:
        for k in _ROW_ID_KEYS:
            v = row.get(k)
            if v not in (None, ""):
                name = f"{_ko_field(k)} {v}"
                fallback_id_key = k
                break
    name = name or "?"

    extras: List[str] = []
    for k, v in row.items():
        if k in _ROW_NAME_KEYS or v is None:
            continue
        if fallback_id_key is not None and k == fallback_id_key:
            continue
        if isinstance(v, bool):
            continue
        if isinstance(v, dict):
            continue
        ko = _ko_field(k)
        if isinstance(v, list):
            if v and all(isinstance(x, (str, int, float)) for x in v):
                extras.append(f"{ko} {', '.join(str(x) for x in v[:5])}")
            continue
        if isinstance(v, (int, float, str)):
            sv = str(v)
            if len(sv) <= 80:
                extras.append(f"{ko} {sv}")

    extras = extras[:_ROW_MAX_FIELDS]
    return f"- {name}: " + ", ".join(extras) if extras else f"- {name}"


def _build_objective_narrative(tool_id: str, tool_title: str, result: Dict[str, Any]) -> str:
    """결정적 사실 나열 — LLM 호출 없음, 추측·해석 절대 발생 X.

    [본문] 에 result 의 모든 행 데이터를 노출. INSUFFICIENT 면 빈 문자열.
    """
    summary = result.get("summary") or {}
    if summary.get("mode") == "INSUFFICIENT":
        return ""

    label = f"{tool_id} {tool_title}".strip() if tool_title else tool_id
    sections: List[str] = [f"분석 {label}"]

    # 1) summary 의 단일 수치·문자열
    summary_lines: List[str] = []
    for k, v in summary.items():
        if k in ("mode", "skipped_reason") or v is None or isinstance(v, bool):
            continue
        if isinstance(v, (int, float, str)) and (str(v).strip() if isinstance(v, str) else True):
            ko = _SUMMARY_LABEL_KR.get(k, k)
            summary_lines.append(f"- {ko}: {v}")
    if summary_lines:
        sections.append("")
        sections.append("[요약]")
        sections.extend(summary_lines)

    # 2) 명단별 — 리스트별 최대 _LIST_ROW_CAP 행만 노출 (전체는 '보고서 생성' 시 풀 노출)
    for key, ko_label in _LIST_LABEL_KR:
        items = result.get(key)
        if not isinstance(items, list) or not items:
            continue
        sections.append("")
        sections.append(f"[{ko_label}] (총 {len(items)}건)")
        shown = 0
        for row in items[:_LIST_ROW_CAP]:
            if isinstance(row, dict):
                sections.append(_render_row_inline(row))
                shown += 1
        if len(items) > shown:
            sections.append(f"... 외 {len(items) - shown}건 (전체는 보고서에서 확인)")

    # summary/명단 모두 비었으면 본문 자체 미노출
    if len(sections) == 1:
        return ""

    return "[본문]\n" + "\n".join(sections)


def generate_short_response(state: AnalysisState) -> Dict[str, Any]:
    if state.get("error"):
        return {"response": {"intent": "analyze", "error": state["error"]}}

    result = state.get("tool_result", {})
    query = state["user_query"]
    tool_id = state.get("tool_id", "")
    tool_meta = TOOL_REGISTRY.get(tool_id, {})

    # raw JSON 대신 정제된 요약 — LLM 컨텍스트 절약 + 품질 ↑
    result_summary = _summarize_result_for_llm(result)

    # Reasoner 가 만든 복합 narrative 가 있으면 LLM 재호출 없이 그대로 사용
    pre_built = state.get("reasoner_narrative")

    # 표본 부족 — LLM 우회. narrative 비움 → FE amber 배너만.
    summary = result.get("summary") or {}
    if pre_built:
        narrative = pre_built
        logger.info(f"narrative reasoner 재사용: len={len(narrative)}")
    elif summary.get("mode") == "INSUFFICIENT":
        narrative = ""
        logger.info("INSUFFICIENT — narrative 생성 생략")
    elif tool_id == "I-12":
        # I-12 종합 — 데이터가 풍부하고 구조화돼 있어 LLM 이 압축·축약하면 손실.
        # 결정적 빌더로 전 7개 분석 수치를 모두 글로 풀어낸 후 [AI 박스] 만 LLM 으로 짧게 요약.
        narrative = _build_i12_narrative_full(result)
        logger.info(f"I-12 narrative 결정적 생성: len={len(narrative)}")
    else:
        llm = get_llm()
        response = llm.invoke(SHORT_PROMPT.format(result=result_summary, query=query))
        narrative = response.content
        logger.info(f"narrative LLM 생성: len={len(narrative)}")

    offer_report = _should_offer_report(query, result)
    logger.info(f"보고서 제안 여부: {offer_report} (query='{query[:40]}...')")

    out_response: Dict[str, Any] = {
        "intent": "analyze",
        "form": "report" if offer_report else "short",
        "indicator_id": tool_id,
        "report_id": result.get("report_id"),
        "title": tool_meta.get("title"),
        "narrative": narrative,
        "summary": result.get("summary"),
    }
    if offer_report:
        out_response["next_action"] = {
            "type": "hitl",
            "stage": "ask_generate",
            "question": "보고서 생성할까요?",
            "options": ["yes", "no"],
        }

    # 멀티턴 이력에 이번 턴 추가 (다음 발화의 resolve_context 가 참조)
    history = list(state.get("conversation_history") or [])
    history.append({
        "user": state.get("original_query") or query,
        "indicator_id": tool_id,
        "summary": result_summary[:400],   # 너무 길면 컷
    })
    if len(history) > _HISTORY_MAX_TURNS:
        history = history[-_HISTORY_MAX_TURNS:]

    return {
        "response_form": "report" if offer_report else "short",
        "response": out_response,
        "conversation_history": history,
    }


# ─── I-12 종합 — preview 글 요약 빌더 ───
def _build_i12_preview_text(result: Dict[str, Any]) -> str:
    """I-12 preview — 차트·표 없이 글로만 핵심 요약."""
    overview = result.get("summary_overview", {}) or {}
    matrix = result.get("section_2_dept_matrix", []) or []
    candidates = result.get("section_3_emp_candidates", []) or []
    priorities = result.get("section_5_priorities", []) or []
    safety_sum = overview.get("safety", {}) or {}
    eval_sum = overview.get("evaluator_pattern", {}) or {}
    season = overview.get("season", {}) or {}

    strong = sum(1 for c in candidates if c.get("strength") == "강함")
    medium = sum(1 for c in candidates if c.get("strength") == "중간")
    single = sum(1 for c in candidates if c.get("strength") == "단일")
    underpaid_n = overview.get("underpaid_count", 0)
    unfair_n = overview.get("unfair_count", 0)
    violation_n = safety_sum.get("total_violation_emps", 0)
    eval_outliers = eval_sum.get("n_candidates", 0)

    lines = [
        f"**{season.get('name', '미지정')} 시즌 전사 진단 요약**",
        "",
        f"분석 대상 부서 {len(matrix)}개에서 도출된 결과를 정리합니다. "
        f"보상 누락 후보 {underpaid_n}명, 부당 보상 후보 {unfair_n}명, "
        f"법규 위반 사원 {violation_n}명, 평가자 이상치 {eval_outliers}명이 식별되었습니다.",
        "",
        f"위 신호들이 동시에 잡힌 사원은 총 {len(candidates)}명이며, "
        f"즉시 면담이 필요한 강 시그널 {strong}명, 검토 안건인 중 시그널 {medium}명, "
        f"관찰 대상인 단일 시그널 {single}명으로 구성되어 있습니다.",
    ]

    if priorities:
        lines.append("")
        lines.append("**HR 검토 우선순위**")
        for p in priorities:
            lines.append(f"- [레벨 {p['level']}] {p['category']} — {p['summary']}")

    lines.append("")
    lines.append("_보고서를 생성하면 부서별 매트릭스 표·후보 사원 명단·시각화 차트를 함께 확인할 수 있습니다._")
    return "\n".join(lines)


# ─── I-12 종합 — 결정적 narrative 빌더 (LLM 우회) ───
#
# 7개 분석 데이터를 빠짐없이 글로 풀어낸다. 작은 LLM 이 압축·축약·환각하는 위험 0.
# [본문] 에 모든 수치, [AI 박스] 에 핵심 수치 한 문장만.
def _build_i12_narrative_full(result: Dict[str, Any]) -> str:
    overview = result.get("summary_overview", {}) or {}
    matrix = result.get("section_2_dept_matrix", []) or []
    candidates = result.get("section_3_emp_candidates", []) or []
    evaluator_section = result.get("section_4_evaluator", {}) or {}
    priorities = result.get("section_5_priorities", []) or []
    source = result.get("source_analyses", {}) or {}
    season = overview.get("season", {}) or {}
    safety_sum = overview.get("safety", {}) or {}
    eval_sum = overview.get("evaluator_pattern", {}) or {}
    workload_sum = overview.get("workload", {}) or {}
    grade_dist_sum = overview.get("dept_grade_distribution", {}) or {}

    # ─── 사원 후보 강도별 카운트 ───
    strong = sum(1 for c in candidates if c.get("strength") == "강함")
    medium = sum(1 for c in candidates if c.get("strength") == "중간")
    single = sum(1 for c in candidates if c.get("strength") == "단일")

    # ─── 부서 매트릭스 라벨 카운트 ───
    def _count_label(key: str) -> Dict[str, int]:
        c: Dict[str, int] = {}
        for d in matrix:
            v = d.get(key) or "데이터 없음"
            c[v] = c.get(v, 0) + 1
        return c
    grade_label_counts = _count_label("label_grade_dist")
    workload_label_counts = _count_label("label_workload")
    safety_label_counts = _count_label("label_safety")

    # ─── 핵심 수치 ───
    underpaid_n = overview.get("underpaid_count", 0)
    unfair_n = overview.get("unfair_count", 0)
    violation_n = safety_sum.get("total_violation_emps", 0)
    violation_dept_n = safety_sum.get("violation_dept_count", 0)
    eval_outliers = eval_sum.get("n_candidates", 0)
    eval_total = eval_sum.get("n_evaluators", 0)
    overload_n = workload_sum.get("overload_dept_count", 0)
    biased_dept_n = grade_dist_sum.get("biased_dept_count", 0)

    body: List[str] = []
    body.append(f"**§1. 분석 시즌 — {season.get('name', '미지정')} (id={season.get('id', '?')})**")
    body.append(f"- 분석 부서: {len(matrix)}개")
    body.append(f"- 사원 후보 통합: {len(candidates)}명 (강 {strong} · 중 {medium} · 단일 {single})")
    body.append(f"- 평가자 풀: {eval_total}명, 이상치 후보 {eval_outliers}명")
    body.append("")

    body.append("**§2. 7개 분석 결과 요약**")
    body.append(f"- #1 보상-성과 정합성 — 부당 보상 후보 {unfair_n}명")
    body.append(f"- #2 우수인재 보상 누락 — 후보 {underpaid_n}명")
    body.append(f"- #4 부서별 등급 분포 — 편중 부서 {biased_dept_n}개")
    body.append(f"- #5 평가자 점수 분포 — 이상치 평가자 {eval_outliers}명 / 전체 {eval_total}명")
    body.append(f"- #6 등급 변동 패턴 — 추적 사원 {(source.get('I-06') or {}).get('summary', {}).get('n_employees', '?')}명")
    body.append(f"- #7 워라밸 — 과부하 부서 {overload_n}개")
    body.append(f"- #9 산업안전 — 법규 위반 사원 {violation_n}명, 위반 발생 부서 {violation_dept_n}개")
    body.append("")

    if matrix:
        body.append(f"**§3. 부서별 진단 매트릭스 ({len(matrix)}개 부서) — 라벨 분포**")
        if grade_label_counts:
            body.append("- 등급 분포: " + ", ".join(f"{k} {v}개" for k, v in grade_label_counts.items()))
        if workload_label_counts:
            body.append("- 워라밸: " + ", ".join(f"{k} {v}개" for k, v in workload_label_counts.items()))
        if safety_label_counts:
            body.append("- 산업안전: " + ", ".join(f"{k} {v}개" for k, v in safety_label_counts.items()))
        body.append("")

    if candidates:
        body.append(f"**§4. 사원 후보 통합 ({len(candidates)}명)**")
        body.append(f"- 강 시그널 (3개 분석 동시 적발): {strong}명")
        body.append(f"- 중 시그널 (2개 분석 동시 적발): {medium}명")
        body.append(f"- 단일 시그널 (1개 분석 적발): {single}명")
        # 강·중 시그널 사원은 명단 노출 (단일은 분량 폭주 방지로 생략)
        for sev in ("강함", "중간"):
            picked = [c for c in candidates if c.get("strength") == sev][:10]
            if picked:
                body.append(f"  - {sev} 시그널 사원 (상위 {len(picked)}명):")
                for c in picked:
                    sigs = ", ".join(c.get("signals") or [])
                    body.append(f"    · {c.get('emp_name', '?')} ({c.get('dept_name', '?')} · {c.get('grade_name', '?')}) — 신호: {sigs}")
        body.append("")

    eval_cands = evaluator_section.get("candidates") or []
    if eval_cands:
        body.append(f"**§5. 평가자 진단 — 이상치 평가자 ({len(eval_cands)}명)**")
        for ev in eval_cands[:10]:
            reasons = ", ".join(ev.get("flag_reasons") or [])
            body.append(
                f"- {ev.get('evaluator_name', '?')} "
                f"({ev.get('dept_name', '?')} · 피평가자 {ev.get('n_evaluatees', '?')}명) "
                f"— 평균 {ev.get('mean_score', '?')}, Z {ev.get('mean_z', '?')}"
                + (f" — 사유: {reasons}" if reasons else "")
            )
        body.append("")

    detail1 = (source.get("I-01") or {}).get("summary", {})
    if detail1:
        body.append("**§6. 직급별 단기 변별력 (#1 detail1) — 라벨 카운트**")
        for k, v in detail1.items():
            if isinstance(v, (int, str, float)):
                body.append(f"- {k}: {v}")
        body.append("")

    if priorities:
        body.append("**§7. HR 검토 우선순위**")
        for p in priorities:
            details_n = len(p.get("details") or [])
            body.append(
                f"- [레벨 {p.get('level', '?')}] {p.get('category', '?')} "
                f"— {p.get('summary', '')} (관련 {details_n}건)"
            )
        body.append("")

    # ─── AI 박스 (객관 수치 한 문장) ───
    ai_box_line = (
        f"강 시그널 {strong}명, 중 시그널 {medium}명, 단일 {single}명. "
        f"법규 위반 사원 {violation_n}명, 평가자 이상치 {eval_outliers}명, "
        f"보상 누락 후보 {underpaid_n}명, 부당 보상 후보 {unfair_n}명."
    )

    return "[본문]\n" + "\n".join(body) + "\n\n[AI 박스]\n" + ai_box_line


# ─── 노드 5b: report 빌드 (차트 + 마크다운 미리보기) ───
def build_report(state: AnalysisState) -> Dict[str, Any]:
    if state.get("error"):
        return {"response": {"intent": "analyze", "error": state["error"]}}

    result = state.get("tool_result", {})
    indicator_id = state.get("tool_id", "")
    # preview = 글 위주. I-12 종합은 차트 없음, 다른 지표는 기존대로.
    chart_specs = build_chart_specs(result, mode="preview")

    response: Dict[str, Any] = {
        "intent": "analyze",
        "form": "report",
        "indicator_id": indicator_id,
        "report_id": result.get("report_id"),
        "title": TOOL_REGISTRY.get(indicator_id, {}).get("title"),
        "summary": result.get("summary"),
        "chart_specs": chart_specs,
        "next_action": {
            "type": "hitl",
            "stage": "ask_generate",
            "question": "보고서 문서를 생성할까요?",
            "options": ["yes", "no"],
        },
    }

    # I-12 종합: 글 위주 preview — raw_result/표 데이터 제외, preview_text 만 노출
    # 다른 지표: 기존대로 raw_result 포함 (FE 가 표 렌더)
    if indicator_id == "I-12":
        response["preview_text"] = _build_i12_preview_text(result)
    else:
        response["raw_result"] = result

    return {"chart_specs": chart_specs, "response": response}


# ─── 노드 6: gate_generate (HITL #1 마커, interrupt_before 대상) ───
def gate_generate(state: AnalysisState) -> Dict[str, Any]:
    # LangGraph 는 노드가 최소 한 채널은 써야 해서 결정값을 그대로 재기록(idempotent).
    decision = state.get("user_decision_generate", "no")
    logger.info(f"HITL #1 결정: {decision}")
    return {"user_decision_generate": decision}


def route_after_generate_gate(state: AnalysisState) -> str:
    return "generate_doc" if state.get("user_decision_generate") == "yes" else END


# ─── 보고서 전용 서술형 narrative 빌더 ───
# 즉시 화면(generate_short_response) 은 결정적 사실 나열만 하고,
# 보고서를 만들 때만 LLM 으로 서술형 요약을 따로 생성해 [AI 참고] 섹션에 끼워넣는다.
def _build_report_narrative(tool_id: str, result: Dict[str, Any]) -> str:
    summary = result.get("summary") or {}
    if summary.get("mode") == "INSUFFICIENT":
        return ""
    try:
        result_summary = _summarize_result_for_llm(result)
        # query 자리는 도구 설명으로 대체 — 보고서엔 사용자 발화가 아닌 분석 자체를 서술해야 자연스러움
        tool_meta = TOOL_REGISTRY.get(tool_id, {})
        pseudo_query = tool_meta.get("title") or tool_id or "분석"
        llm = get_llm()
        response = llm.invoke(SHORT_PROMPT.format(result=result_summary, query=pseudo_query))
        text = response.content.strip()
        # [본문] / [AI 박스] 마커 제거 → 마크다운의 'AI 참고' 섹션 안에 붙음
        for marker in ("[본문]", "【본문】", "[AI 박스]", "【AI 박스】", "## AI 박스", "AI 박스:", "AI 참고:"):
            text = text.replace(marker, "")
        return text.strip()
    except Exception as e:
        logger.warning(f"보고서 narrative LLM 실패 — 빈 값으로 진행: {e}")
        return ""


# ─── 노드 7: 문서 생성 (markdown 파일) ───
def generate_doc(state: AnalysisState) -> Dict[str, Any]:
    result = state.get("tool_result", {})
    tool_id = state.get("tool_id", "")
    # 보고서 전용 서술형 narrative — 즉시 응답의 결정적 텍스트 대신 LLM 서술 사용
    narrative = _build_report_narrative(tool_id, result)

    md = build_markdown(result, narrative=narrative)
    # 보고서 생성 시점에 차트 풀세트 주입 (I-12 종합 등)
    full_charts = build_chart_specs(result, mode="full")
    logger.info(f"문서 생성: md={len(md)}자, 차트 {len(full_charts)}개, narrative={len(narrative)}자")

    # 채팅 화면 안에서 시각화 — chart_specs + raw_result 만 노출
    # 다운로드 페이로드(html_report, markdown_report) 는 응답에 안 실음
    # markdown_report 는 save_to_inbox 가 내부적으로만 사용
    response = dict(state.get("response", {}))
    response.pop("preview_text", None)  # 글 요약은 풀 시각화로 대체
    response.update({
        "chart_specs": full_charts,
        "raw_result": result,
        "next_action": {
            "type": "hitl",
            "stage": "ask_save",
            "question": "내 파일함의 'AI 보고서' 폴더에 저장할까요?",
            "options": ["yes", "no"],
        },
    })
    return {
        "markdown_report": md,
        "chart_specs": full_charts,
        "response": response,
    }


# ─── 노드 8: gate_save (HITL #2 마커) ───
def gate_save(state: AnalysisState) -> Dict[str, Any]:
    decision = state.get("user_decision_save", "no")
    logger.info(f"HITL #2 결정: {decision}")
    return {"user_decision_save": decision}


def route_after_save_gate(state: AnalysisState) -> str:
    return "save_to_inbox" if state.get("user_decision_save") == "yes" else END


# ─── 노드 9: 내 파일함 저장 (collaboration-service /internal/filevault POST) ───
def save_to_inbox(state: AnalysisState) -> Dict[str, Any]:
    md = state.get("markdown_report") or ""
    company_id = state.get("auth_company_id")
    user_emp_id = state.get("auth_user_emp_id")

    if not company_id or not user_emp_id:
        logger.error("저장 실패: 인증 헤더 없음")
        response = dict(state.get("response", {}))
        response["save_error"] = "인증 정보(X-User-Company, X-User-Id) 없음"
        response.pop("next_action", None)
        return {"save_error": "인증 정보 없음", "response": response}

    result = state.get("tool_result", {})
    indicator_id = state.get("tool_id", "I-XX")
    report_id_str = result.get("report_id", "")
    title = f"{report_id_str} {TOOL_REGISTRY.get(indicator_id, {}).get('title', '분석 보고서')}"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_name = f"report_{indicator_id}_{timestamp}.md"

    upload_result = upload_ai_report(
        company_id=company_id,
        user_emp_id=int(user_emp_id),
        file_bytes=md.encode("utf-8"),
        file_name=file_name,
        title=title,
        content_type="text/markdown",
    )

    response = dict(state.get("response", {}))
    response.pop("next_action", None)

    if "file_id" in upload_result and upload_result["file_id"] is not None:
        response["saved_report_id"] = upload_result["file_id"]
        response["saved_folder_id"] = upload_result.get("folder_id")
        response["saved_folder_name"] = upload_result.get("folder_name")
        response["save_status"] = "success"
        return {
            "saved_report_id": upload_result["file_id"],
            "response": response,
        }
    err = upload_result.get("error", "unknown error")
    response["save_error"] = err
    response["save_status"] = "failed"
    return {"save_error": err, "response": response}


# ─── 노드 10: RAG 검색 (explain) ───
#
# 발화 카테고리 추론 — RAG docs 폴더 4종 중 어디서 답을 찾을지 좁힌다.
# 안 좁히면 "규칙들은?" 같은 짧은 발화에 indicators/ 의 분석 도구 내부 정의
# (단기 변별력, 부당 보상 후보 등) 가 끼어들어 사용자가 묻는 "평가 운영 규칙" 과
# 무관한 답이 나옴.
#
# 우선순위:
#   1. 평가 운영(rules / usage / procedures): 강제분포, 가중치, 시즌 단계, 보정,
#      평가자 매핑, KPI 등록, 등급 산정·확정 — HR 이 시즌 시작 전·중에 다루는 항목
#   2. 분석 도구(indicators): 보상 누락, 워라밸, 결합 강도 등 분석 의미 정의
_PLATFORM_RULE_KEYWORDS = [
    "규칙", "기준", "정의", "설정", "옵션", "정책",
    "강제분포", "강제 분포", "분포", "가중치", "비율",
    "시즌", "단계", "절차", "흐름", "운영", "프로세스",
    "보정", "calibration", "확정", "최종", "산정", "잠금",
    "평가자", "권한", "이의신청",
    "Z-score", "Z점수", "표편차",
    "KPI", "OKR", "목표", "등록",
    "휴직", "복직", "이동", "신규",
    "공식", "자동", "스케줄",
    # 성과 평가 시스템 자체 흐름 질문 — procedures/usage 로 가야 함
    # (사용자가 "성과 어떻게 평가해", "평가 과정", "성과 진행" 같이 물으면 시스템 플로우)
    "성과 평가", "평가 과정", "평가 흐름", "평가 단계", "평가 진행",
    "성과 과정", "성과 흐름", "성과 진행",
    "어떻게 평가", "어떻게 진행", "어떻게 이루어", "어떻게 동작",
    "자기평가", "상위자평가", "상위평가", "팀장 평가",
    "등급 산정", "등급산정", "등급 부여", "등급 결정",
    "결과 확정", "최종 확정",
]

_INDICATOR_KEYWORDS = [
    "보상 누락", "우수인재", "워라밸", "산업안전",
    "부당 보상", "단기 변별력", "장기 변별력",
    "결합 강도", "신호", "후보", "정합성",
    "편향", "이상치", "변동 패턴",
]


def _infer_category_filter(query: str) -> Optional[List[str]]:
    """발화에서 RAG category 선호도 추론."""
    q = query.lower()
    has_platform = any(kw.lower() in q for kw in _PLATFORM_RULE_KEYWORDS)
    has_indicator = any(kw.lower() in q for kw in _INDICATOR_KEYWORDS)

    if has_platform and not has_indicator:
        # 운영 규칙 질문 — 분석 도구 내부 정의 제외
        return ["rules", "usage", "procedures"]
    if has_indicator and not has_platform:
        # 분석 도구 의미 질문 — indicators 만
        return ["indicators"]
    return None  # 둘 다 / 둘 다 아님 → 전체 검색


def rag_search_node(state: AnalysisState) -> Dict[str, Any]:
    query = state["user_query"]

    # GraphRAG 분기 — 복합 시그널(관계/비교/전체)이 있고 그래프 색인돼있으면 GraphRAG 사용
    try:
        from analysis.rag.graph.search import graph_search, is_complex_query
        if is_complex_query(query):
            mode = "global" if any(w in query for w in ("전체", "흐름", "어떻게 돌아")) else "local"
            gr = graph_search(query, mode=mode)
            if gr and gr.get("answer"):
                # GraphRAG 답변을 단일 청크처럼 rag_context 로 래핑 → 기존 흐름 호환
                chunk = {
                    "content": gr["answer"],
                    "section": f"GraphRAG ({mode})",
                    "doc_title": "그래프 종합 추론",
                    "category": "graphrag",
                    "_rrf_score": 1.0,
                }
                logger.info(f"GraphRAG 사용 (mode={mode})")
                return {"rag_context": [chunk]}
            logger.info("GraphRAG 비활성 또는 답변 없음 → 일반 RAG 폴백")
    except Exception as e:
        logger.warning(f"GraphRAG 호출 실패 → 일반 RAG 폴백: {e}")

    # 기본 — 기존 BM25+벡터+RRF 검색
    cat_filter = _infer_category_filter(query)
    if cat_filter:
        logger.info(f"RAG category 필터: {cat_filter}")
    try:
        chunks = rag_search(query, top_k=5, category_filter=cat_filter)
        if not chunks and cat_filter:
            logger.info("RAG 카테고리 필터 결과 0건 → 전체 검색으로 폴백")
            chunks = rag_search(query, top_k=5)
        logger.info(f"RAG 검색: {len(chunks)} 청크")
        return {"rag_context": chunks}
    except Exception as e:
        logger.exception("RAG 검색 실패")
        return {"rag_context": [], "error": f"RAG 오류: {e}"}


# ─── 노드 11: explain 응답 (few-shot 보강) ───
#
# ⚠️ 중요한 구분 — 이 시스템에는 두 종류의 정보가 RAG 에 들어있다.
#   A. 시스템 운영 자료 (procedures / usage / rules)
#      = 사용자가 "어떻게 평가하는지" 물으면 답해야 할 실제 시스템 동작.
#   B. 분석 도구 정의 (indicators)
#      = #1, #2, ... 사후 데이터 진단 도구의 정의. 시스템 운영 플로우가 아님.
#      ⚠ 분석 도구는 "성과가 이루어지는 시스템" 이 아니다. 별개의 audit 도구.
#
# 작은 모델이 검색 결과를 분간 못 하고 그대로 옮기는 사고 차단 — 프롬프트에서 명시.
# EXPLAIN_PROMPT → analysis/prompts/explain.py


# ─── 상세 explain 프롬프트 (자세히·이어서·구체적 요청 시) ───
#
# 길이 제한 없음. 단계·항목·예시·주의사항까지 풍부하게.
# 직전 답변이 있으면 그대로 반복하지 말고 "더 깊이" 풀어쓰도록 명시.
# EXPLAIN_PROMPT_DETAILED → analysis/prompts/explain.py


# ─── 발화에서 "상세 답변 원함" 추론 ───
#
# - "자세히", "더 자세히", "디테일", "구체적", "상세히", "이어서" 등 명시 키워드
# - 직전 턴이 EXPLAIN 이고 짧은 후속 (지시어 위주) 이면 elaboration 으로 간주
_DETAIL_KEYWORDS = [
    "자세히", "더 자세히", "더자세히",
    "디테일", "디테일하게",
    "상세히", "상세하게", "구체적", "구체적으로",
    "이어서", "이어서 말", "이어 말",
    "더 알려", "더알려", "더 보여", "더보여", "더 설명",
    "예시", "예를 들어", "예를들어",
    "단계별", "step by step",
    "전부", "다 알려", "다알려",
]


def _wants_detail(query: str, history: List[Dict[str, Any]]) -> bool:
    """발화가 상세 답변 요청인지 판단."""
    q_lower = query.lower()
    if any(kw.lower() in q_lower for kw in _DETAIL_KEYWORDS):
        return True
    # 직전 턴이 EXPLAIN 이고 짧은 후속 (예: "더?", "그래서?") 이면 elaboration
    last = history[-1] if history else None
    if last and last.get("indicator_id") == "EXPLAIN" and len(query.strip()) <= 25:
        return True
    return False


# ─── 시각화(다이어그램) 의도 감지 ───
# RAG 문서에 mermaid 블록이 들어있고, 발화에 시각화 키워드 있으면 mermaid 보존 보장.
_VISUAL_KEYWORDS = [
    "그림", "그림으로", "도식", "도식화", "도표",
    "다이어그램", "diagram", "mermaid",
    "흐름도", "플로우차트", "flow", "flowchart",
    "시각적", "시각화",
    "보여줘", "보여 줘",  # 약하지만 explain 분기에선 시각화 의도로 충분
]


def _wants_visual(query: str) -> bool:
    """발화가 시각화·다이어그램 요청인지 판단."""
    q_lower = query.lower()
    return any(kw.lower() in q_lower for kw in _VISUAL_KEYWORDS)


_MERMAID_BLOCK_RE = _re_pii.compile(r"```mermaid\s*\n([\s\S]*?)```", _re_pii.MULTILINE)


def _extract_mermaid_blocks(text: str) -> List[str]:
    """텍스트에서 ```mermaid``` 블록만 추출."""
    if not text:
        return []
    return _MERMAID_BLOCK_RE.findall(text)


def _ensure_mermaid_in_answer(answer: str, chunks: List[Dict[str, Any]]) -> str:
    """LLM 답변에 mermaid 가 있으면 그대로, 없으면 검색 청크에서 가장 첫 mermaid 를 답 끝에 추가.

    작은 LLM 이 mermaid 블록을 누락하거나 깨뜨리는 사고를 결정적으로 차단.
    """
    if "```mermaid" in (answer or ""):
        return answer  # LLM 이 이미 포함시킴
    for c in chunks:
        blocks = _extract_mermaid_blocks(c.get("content") or "")
        if blocks:
            return f"{answer}\n\n```mermaid\n{blocks[0].strip()}\n```"
    return answer  # 청크에도 없으면 그대로


def generate_explain_response(state: AnalysisState) -> Dict[str, Any]:
    query = state["user_query"]
    chunks = state.get("rag_context") or []

    if not chunks:
        narrative = "관련 분석 기준 문서를 찾지 못했습니다."
        history = list(state.get("conversation_history") or [])
        history.append({
            "user": state.get("original_query") or query,
            "indicator_id": "EXPLAIN",
            "summary": "관련 문서 미발견",
        })
        if len(history) > _HISTORY_MAX_TURNS:
            history = history[-_HISTORY_MAX_TURNS:]
        return {
            "response": {
                "intent": "explain",
                "form": "short",
                "narrative": narrative,
            },
            "conversation_history": history,
        }

    history_pre = list(state.get("conversation_history") or [])
    detailed = _wants_detail(query, history_pre)
    visual = _wants_visual(query)

    # 상세 모드면 청크 더 많이, 아니면 기본 3개
    chunk_count = 6 if detailed else 3
    context_parts = [c["content"] for c in chunks[:chunk_count]]
    context = "\n\n---\n\n".join(context_parts)

    # explain 은 RAG 청크(공개 문서) + 발화만 LLM 에 전달.
    # safe_invoke 가 PII 감지 시 cloud 차단 + EXAONE 폴백.
    if detailed:
        # 직전 EXPLAIN 답변 있으면 prompt 에 함께 넣어 반복 방지·확장 유도
        prev_answer = ""
        for h in reversed(history_pre):
            if h.get("indicator_id") == "EXPLAIN":
                prev_answer = h.get("summary") or ""
                break
        if not prev_answer:
            prev_answer = "(직전 답변 없음 — 처음부터 풍부하게 설명)"
        logger.info(f"explain 모드: DETAILED (chunks={chunk_count}, visual={visual})")
        prompt = EXPLAIN_PROMPT_DETAILED.format(
            context=context,
            query=query,
            prev_answer=prev_answer[:600],
        )
    else:
        logger.info(f"explain 모드: BRIEF (chunks={chunk_count}, visual={visual})")
        prompt = EXPLAIN_PROMPT.format(context=context, query=query)

    # 시각화 의도면 prompt 끝에 mermaid 보존 지시 추가 — LLM 이 답에 ```mermaid``` 블록 그대로 포함하게.
    if visual:
        prompt += (
            "\n\n[추가 지시 — 시각화 요청]\n"
            "검색 결과에 ```mermaid``` 코드 블록이 있으면 답변에 **반드시 그대로** 포함하세요. "
            "재작성·요약·번역 X. 코드 블록 펜스(```) 와 'mermaid' 표기까지 그대로."
        )

    response = safe_invoke(prompt, purpose="explain")
    answer_text = response.content or ""

    # 시각화 의도인데 LLM 이 mermaid 누락했으면 결정적으로 청크에서 추출해 추가
    if visual:
        answer_text = _ensure_mermaid_in_answer(answer_text, chunks[:chunk_count])

    # 후속 elaboration 이 직전 답변을 참조할 수 있도록 더 길게 보관 (요약·반복 방지용)
    history = list(state.get("conversation_history") or [])
    history.append({
        "user": state.get("original_query") or query,
        "indicator_id": "EXPLAIN",
        "summary": answer_text[:800],
    })
    if len(history) > _HISTORY_MAX_TURNS:
        history = history[-_HISTORY_MAX_TURNS:]

    return {
        "response": {
            "intent": "explain",
            "form": "short",
            "narrative": answer_text,
        },
        "conversation_history": history,
        # BRIEF 답이었으면 grade_answer 통과 후 "더 자세히?" HITL 게이트로 라우팅
        "is_brief_explain": (not detailed),
    }


# ─── CRAG (Corrective RAG) 자기수정 루프 ───
# explain 분기에 retrieve → grade → (필요 시) rewrite → retrieve 사이클 추가.
# 무한루프 방지: rewrite 최대 _MAX_REWRITES 회.
_MAX_REWRITES = 2


# GRADE_CHUNKS_PROMPT → analysis/prompts/crag.py


# GRADE_ANSWER_PROMPT → analysis/prompts/crag.py


# REWRITE_QUERY_PROMPT → analysis/prompts/crag.py


def grade_chunks_node(state: AnalysisState) -> Dict[str, Any]:
    """검색 청크가 질문에 답할 만한 정보를 담고 있는지 판단."""
    chunks = state.get("rag_context") or []
    if not chunks:
        logger.info("청크 평가: irrelevant (청크 0개)")
        return {"chunks_grade": "irrelevant"}

    query = state["user_query"]
    context = "\n---\n".join(c["content"][:500] for c in chunks[:3])
    try:
        prompt = GRADE_CHUNKS_PROMPT.format(query=query, context=context)
        response = safe_invoke(prompt, purpose="grade_chunks")
        verdict = "relevant" if "yes" in (response.content or "").strip().lower() else "irrelevant"
        logger.info(f"청크 평가: {verdict} (rewrite_count={state.get('rewrite_count', 0)})")
        return {"chunks_grade": verdict}
    except Exception as e:
        logger.warning(f"청크 평가 실패 → relevant 가정: {e}")
        return {"chunks_grade": "relevant"}


def grade_answer_node(state: AnalysisState) -> Dict[str, Any]:
    """답변이 질문에 충실히 답하고 청크에 근거하는지 판단."""
    answer = (state.get("response") or {}).get("narrative", "")
    chunks = state.get("rag_context") or []
    if not answer or not chunks:
        logger.info("답변 평가: bad (답변·청크 비어있음)")
        return {"answer_grade": "bad"}

    query = state["user_query"]
    context = "\n---\n".join(c["content"][:500] for c in chunks[:3])
    try:
        prompt = GRADE_ANSWER_PROMPT.format(query=query, context=context, answer=answer[:600])
        response = safe_invoke(prompt, purpose="grade_answer")
        verdict = "good" if "yes" in (response.content or "").strip().lower() else "bad"
        logger.info(f"답변 평가: {verdict} (rewrite_count={state.get('rewrite_count', 0)})")
        return {"answer_grade": verdict}
    except Exception as e:
        logger.warning(f"답변 평가 실패 → good 가정: {e}")
        return {"answer_grade": "good"}


def rewrite_query_node(state: AnalysisState) -> Dict[str, Any]:
    """질문 재작성 + rewrite_count 증가 (rag_search 로 루프백)."""
    query = state["user_query"]
    count = state.get("rewrite_count", 0)
    try:
        prompt = REWRITE_QUERY_PROMPT.format(query=query)
        response = safe_invoke(prompt, purpose="rewrite_query")
        new_query = (response.content or "").strip().split("\n")[0].strip()
        if not new_query:
            new_query = query
        logger.info(f"질문 재작성 #{count + 1}: '{query[:30]}' → '{new_query[:30]}'")
        return {"user_query": new_query, "rewrite_count": count + 1}
    except Exception as e:
        logger.warning(f"재작성 실패 → 원본 유지: {e}")
        return {"rewrite_count": count + 1}


# ─── 노드 12: fallback ───
def generate_fallback_response(state: AnalysisState) -> Dict[str, Any]:
    return {"response": {
        "intent": "unknown",
        "form": "short",
        "narrative": (
            "발화를 분석·설명 의도로 분류하지 못했습니다. "
            "예: '우수인재 보상 누락 보여줘', '워라밸 진단', '평가자 편향 기준이 뭐야'"
        ),
    }}


# ─── 노드: navigate (페이지 이동 안내) ───
#
# 흐름:
#   resolve_page  → target_page 매칭 + 제안 narrative
#                  ↓ (target_page 있음)
#   gate_navigate (HITL: "X 페이지로 이동할까요?", interrupt_before)
#                  ↓ yes                 ↓ no
#   emit_navigation (next_action=nav)   END
#
# target_page 매칭 실패 시 resolve_page 가 안내 narrative 만 남기고 END.
def resolve_page(state: AnalysisState) -> Dict[str, Any]:
    """발화에서 _PAGE_REGISTRY 의 target 페이지 매칭."""
    query = state["user_query"]
    q_lower = query.lower()

    # 키워드 매칭 점수 가장 높은 페이지 선택
    best_key: Optional[str] = None
    best_score = 0
    for page_key, meta in _PAGE_REGISTRY.items():
        score = sum(1 for kw in meta["keywords"] if kw.lower() in q_lower)
        if score > best_score:
            best_score = score
            best_key = page_key

    if not best_key:
        logger.warning(f"navigate 페이지 매칭 실패: '{query[:30]}'")
        return {
            "target_page": None,
            "response": {
                "intent": "navigate",
                "form": "short",
                "narrative": (
                    "어떤 페이지로 이동할지 찾지 못했습니다. "
                    "예: '성과평가 어디서 해?', '분석 규칙 어떻게 조정해?'"
                ),
            },
        }

    meta = _PAGE_REGISTRY[best_key]
    logger.info(f"navigate 매칭: '{query[:30]}' → {best_key} ({meta['url']})")

    narrative = f"{meta['label']} 페이지에서 진행하실 수 있습니다."
    if meta.get("hint"):
        narrative = f"{narrative} {meta['hint']}"

    target_page: Dict[str, Any] = {
        "key": best_key, "url": meta["url"], "label": meta["label"],
    }
    if meta.get("hint"):
        target_page["hint"] = meta["hint"]

    return {
        "target_page": target_page,
        "response": {
            "intent": "navigate",
            "form": "short",
            "narrative": narrative,
        },
    }


def gate_navigate(state: AnalysisState) -> Dict[str, Any]:
    """HITL 마커 — interrupt_before 대상. 결정 기록만."""
    decision = state.get("user_decision_navigate", "no")
    logger.info(f"HITL navigate 결정: {decision}")
    return {"user_decision_navigate": decision}


def emit_navigation(state: AnalysisState) -> Dict[str, Any]:
    """yes 결정 시 — response 에 next_action.navigate 부착 (FE 가 라우팅)."""
    target = state.get("target_page") or {}
    label = target.get("label", "해당")
    hint = target.get("hint")

    narrative = f"{label} 페이지로 이동합니다."
    if hint:
        narrative = f"{narrative} {hint}"

    next_action: Dict[str, Any] = {
        "type": "navigate",
        "url": target.get("url"),
        "label": label,
    }
    if hint:
        next_action["hint"] = hint  # FE 가 toast/안내로 노출 가능

    return {
        "response": {
            "intent": "navigate",
            "form": "short",
            "narrative": narrative,
            "next_action": next_action,
        }
    }


def route_after_navigate_resolve(state: AnalysisState) -> str:
    """target_page 있으면 게이트로, 없으면 안내만 하고 종료."""
    return "gate_navigate" if state.get("target_page") else END


def route_after_navigate_gate(state: AnalysisState) -> str:
    return "emit_navigation" if state.get("user_decision_navigate") == "yes" else END


# ─── 라우터 ───
def route_by_intent(state: AnalysisState) -> str:
    intent = state.get("intent", "unknown")
    if intent == "analyze":
        return "select_tool"
    if intent == "explain":
        return "rag_search"
    if intent == "navigate":
        return "resolve_page"
    return "fallback"


def route_after_short(state: AnalysisState) -> str:
    """generate_short 결과의 보고서 제안 여부로 분기 (_should_offer_report 참조).

    - report → HITL '보고서 생성?' 게이트로 (사용자가 명시 요청 또는 결과 데이터 풍부)
    - short  → 그대로 종료 (단발성 정보 질의)
    """
    return "gate_generate" if state.get("response_form") == "report" else END


def route_after_grade_chunks(state: AnalysisState) -> str:
    """청크 평가 후 분기.

    - relevant         → generate_explain_response (정상)
    - irrelevant + 재시도 가능 → rewrite_query (CRAG 루프)
    - irrelevant + 한도 도달   → generate_explain_response ("정보 없음" 응답)
    """
    grade = state.get("chunks_grade", "relevant")
    count = state.get("rewrite_count", 0)
    if grade == "relevant":
        return "generate_explain_response"
    if count >= _MAX_REWRITES:
        logger.info(f"청크 irrelevant 지만 rewrite 한도({_MAX_REWRITES}) 도달 → 답변 시도")
        return "generate_explain_response"
    return "rewrite_query"


def route_after_grade_answer(state: AnalysisState) -> str:
    """답변 평가 후 분기.

    - good + BRIEF 모드     → gate_detail ("더 자세히?" HITL 게이트)
    - good + DETAILED 모드  → END
    - bad + 재시도 가능     → rewrite_query (CRAG 루프)
    - bad + 한도 도달       → END (현재 답변 그대로)
    """
    grade = state.get("answer_grade", "good")
    count = state.get("rewrite_count", 0)
    if grade == "good":
        # BRIEF 답이고 답변도 OK → 더 자세히 원하는지 사용자에게 물어봄
        if state.get("is_brief_explain"):
            return "gate_detail"
        return END
    if count >= _MAX_REWRITES:
        logger.info(f"답변 bad 지만 rewrite 한도({_MAX_REWRITES}) 도달 → 종료")
        return END
    return "rewrite_query"


# ─── 노드: gate_detail (HITL "더 자세히?" 마커, interrupt_before 대상) ───
def gate_detail(state: AnalysisState) -> Dict[str, Any]:
    decision = state.get("user_decision_detail", "no")
    logger.info(f"HITL detail 결정: {decision}")
    return {"user_decision_detail": decision}


def route_after_detail_gate(state: AnalysisState) -> str:
    return "generate_detailed_explain" if state.get("user_decision_detail") == "yes" else END


# ─── 노드: 자세한 EXPLAIN 답변 (HITL "예" 후) ───
#
# 직전 BRIEF 답변과 같은 RAG 청크를 재활용 (재검색 X) — DETAILED 프롬프트로 깊이 풀어쓰기.
def generate_detailed_explain(state: AnalysisState) -> Dict[str, Any]:
    query = state["user_query"]
    chunks = state.get("rag_context") or []
    if not chunks:
        return {"response": dict(state.get("response") or {})}

    visual = _wants_visual(query)
    chunk_count = 6
    context_parts = [c["content"] for c in chunks[:chunk_count]]
    context = "\n\n---\n\n".join(context_parts)

    # 직전 BRIEF 답변 — DETAILED 프롬프트가 "이미 답한 내용 반복 X" 로 활용
    prev_answer = (state.get("response") or {}).get("narrative") or "(직전 BRIEF 답변 없음)"

    prompt = EXPLAIN_PROMPT_DETAILED.format(
        context=context,
        query=query,
        prev_answer=prev_answer[:600],
    )
    if visual:
        prompt += (
            "\n\n[추가 지시 — 시각화 요청]\n"
            "검색 결과에 ```mermaid``` 코드 블록이 있으면 답변에 **반드시 그대로** 포함하세요. "
            "재작성·요약·번역 X."
        )
    response = safe_invoke(prompt, purpose="explain_detail")
    answer_text = response.content or ""
    if visual:
        answer_text = _ensure_mermaid_in_answer(answer_text, chunks[:chunk_count])

    new_response = dict(state.get("response") or {})
    new_response["narrative"] = answer_text
    new_response["form"] = "short"
    new_response.pop("next_action", None)

    # history 마지막 EXPLAIN 항목 갱신 (BRIEF 답을 DETAILED 로 교체)
    history = list(state.get("conversation_history") or [])
    if history and history[-1].get("indicator_id") == "EXPLAIN":
        history[-1] = {
            **history[-1],
            "summary": answer_text[:800],
        }

    return {
        "response": new_response,
        "conversation_history": history,
        "is_brief_explain": False,  # DETAILED 까지 갔으므로 더 이상 자세히 제안 X
    }


# ─── 그래프 빌드 ───
def build_graph():
    g = StateGraph(AnalysisState)

    g.add_node("resolve_context", resolve_context)
    g.add_node("classify_intent", classify_intent)
    # Multi-tool Agent — planner / executor / reasoner 로 select_tool/execute_tool 대체
    g.add_node("planner", planner_node)
    g.add_node("executor", executor_node)
    g.add_node("reasoner", reasoner_node)
    g.add_node("generate_short", generate_short_response)
    g.add_node("gate_generate", gate_generate)
    g.add_node("generate_doc", generate_doc)
    g.add_node("gate_save", gate_save)
    g.add_node("save_to_inbox", save_to_inbox)
    g.add_node("rag_search", rag_search_node)
    g.add_node("grade_chunks", grade_chunks_node)
    g.add_node("generate_explain_response", generate_explain_response)
    g.add_node("grade_answer", grade_answer_node)
    g.add_node("rewrite_query", rewrite_query_node)
    g.add_node("gate_detail", gate_detail)
    g.add_node("generate_detailed_explain", generate_detailed_explain)
    g.add_node("resolve_page", resolve_page)
    g.add_node("gate_navigate", gate_navigate)
    g.add_node("emit_navigation", emit_navigation)
    g.add_node("fallback", generate_fallback_response)

    # 모든 발화는 먼저 resolve_context — 멀티턴 컨텍스트 해소
    g.set_entry_point("resolve_context")
    g.add_edge("resolve_context", "classify_intent")

    g.add_conditional_edges("classify_intent", route_by_intent, {
        "select_tool": "planner",   # Multi-tool 진입점 (라우터 키는 호환 위해 유지)
        "rag_search": "rag_search",
        "resolve_page": "resolve_page",
        "fallback": "fallback",
    })

    # planner → executor → (단일?) → generate_short / (복합?) → reasoner → generate_short
    g.add_edge("planner", "executor")
    g.add_conditional_edges("executor", route_after_executor, {
        "reasoner": "reasoner",
        "generate_short": "generate_short",
    })
    g.add_edge("reasoner", "generate_short")

    # narrative 길이로 분기: 5줄 미만이면 END, 5줄 이상이면 보고서 생성 HITL
    g.add_conditional_edges("generate_short", route_after_short, {
        "gate_generate": "gate_generate",
        END: END,
    })

    # 보고서 분기 — HITL 두 단계
    g.add_conditional_edges("gate_generate", route_after_generate_gate, {
        "generate_doc": "generate_doc",
        END: END,
    })
    g.add_edge("generate_doc", "gate_save")
    g.add_conditional_edges("gate_save", route_after_save_gate, {
        "save_to_inbox": "save_to_inbox",
        END: END,
    })
    g.add_edge("save_to_inbox", END)

    # explain — CRAG (Corrective RAG) 자기수정 루프
    #   rag_search → grade_chunks → (relevant) generate_explain_response → grade_answer → END
    #                            ↘ (irrelevant) rewrite_query ↻ rag_search
    #                                                   grade_answer (bad) ↗
    #   rewrite_count ≥ _MAX_REWRITES 면 루프 탈출 (현재 결과로 답변 시도 또는 종료)
    g.add_edge("rag_search", "grade_chunks")
    g.add_conditional_edges("grade_chunks", route_after_grade_chunks, {
        "generate_explain_response": "generate_explain_response",
        "rewrite_query": "rewrite_query",
    })
    g.add_edge("generate_explain_response", "grade_answer")
    g.add_conditional_edges("grade_answer", route_after_grade_answer, {
        "rewrite_query": "rewrite_query",
        "gate_detail": "gate_detail",
        END: END,
    })
    g.add_edge("rewrite_query", "rag_search")

    # explain HITL — "더 자세히 설명해드릴까요?"
    g.add_conditional_edges("gate_detail", route_after_detail_gate, {
        "generate_detailed_explain": "generate_detailed_explain",
        END: END,
    })
    g.add_edge("generate_detailed_explain", END)

    # navigate — 페이지 매칭 → HITL 이동 확인 → next_action 부착
    g.add_conditional_edges("resolve_page", route_after_navigate_resolve, {
        "gate_navigate": "gate_navigate",
        END: END,
    })
    g.add_conditional_edges("gate_navigate", route_after_navigate_gate, {
        "emit_navigation": "emit_navigation",
        END: END,
    })
    g.add_edge("emit_navigation", END)

    # fallback
    g.add_edge("fallback", END)

    # HITL — gate 노드 실행 전 인터럽트
    return g.compile(
        checkpointer=_build_checkpointer(),
        interrupt_before=["gate_generate", "gate_save", "gate_detail", "gate_navigate"],
    )


def _build_checkpointer():
    """체크포인터 선택.

    CHECKPOINT_TYPE 환경변수로 결정 (기본: memory):
      - "memory" : MemorySaver  — 프로세스 RAM. 재시작 시 thread state 소실 (개발용)
      - "sqlite" : SqliteSaver  — SQLite 파일에 영속. 운영 권장
                                  CHECKPOINT_DB_PATH 로 경로 지정 (기본: ./checkpoints.db)

    sqlite 선택했는데 langgraph-checkpoint-sqlite 미설치면 명확한 에러.
    """
    ctype = os.getenv("CHECKPOINT_TYPE", "memory").lower()

    if ctype == "sqlite":
        if not _SQLITE_SAVER_AVAILABLE:
            raise RuntimeError(
                "CHECKPOINT_TYPE=sqlite 인데 langgraph-checkpoint-sqlite 가 설치되지 않았습니다. "
                "requirements.txt 에 'langgraph-checkpoint-sqlite' 추가 후 재빌드."
            )
        import sqlite3
        db_path = os.getenv("CHECKPOINT_DB_PATH", "./checkpoints.db")
        # check_same_thread=False 필수 — FastAPI 는 멀티스레드로 핸들러 호출
        conn = sqlite3.connect(db_path, check_same_thread=False)
        checkpointer = SqliteSaver(conn)
        # 테이블 생성 (idempotent — 이미 있으면 무시)
        checkpointer.setup()
        logger.info(f"체크포인터: SqliteSaver (db={db_path})")
        return checkpointer

    if ctype != "memory":
        logger.warning(f"알 수 없는 CHECKPOINT_TYPE='{ctype}' — memory 로 폴백")
    logger.info("체크포인터: MemorySaver (휘발성, 개발용)")
    return MemorySaver()


# ─── 싱글톤 그래프 ───
# lru_cache 로 thread-safe 한 lazy init. 첫 호출에서만 build_graph() 실행되고
# 이후엔 캐시된 인스턴스 반환. 동시 첫 호출 시에도 한 번만 build.
@lru_cache(maxsize=1)
def get_graph():
    return build_graph()


# 그래프 재귀 한도 — CRAG 루프 + 일반 분기 합쳐 50 이내. 무한 루프 방지.
_GRAPH_RECURSION_LIMIT = 50


# ─── 공개 API ───
def run_analysis(
    user_query: str,
    thread_id: str,
    auth_company_id: Optional[str] = None,
    auth_user_emp_id: Optional[int] = None,
) -> Dict[str, Any]:
    """첫 호출 — interrupt 까지 진행. Redis 캐시 적용 (Phase 1)."""
    # 캐시 조회 — 같은 회사 + 같은 쿼리면 즉시 반환
    from analysis.cache import cache_get, cache_set
    cached = cache_get(user_query, auth_company_id)
    if cached is not None:
        # thread_id 는 호출자 것으로 갱신해서 후속 흐름 호환 유지
        cached = {**cached, "thread_id": thread_id}
        return cached

    graph = get_graph()
    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": _GRAPH_RECURSION_LIMIT,
    }
    initial: AnalysisState = {
        "user_query": user_query,
        "auth_company_id": auth_company_id,
        "auth_user_emp_id": auth_user_emp_id,
    }
    final = graph.invoke(initial, config)
    response = _build_api_response(graph, config, final)

    # 정상 응답만 캐시 저장 (HITL pending / error 는 함수 내부에서 제외)
    cache_set(user_query, auth_company_id, response)
    return response


# resume_analysis: stage → state 키 매핑 (if/elif 분기 대체)
_RESUME_STATE_KEY = {
    "generate": "user_decision_generate",
    "save":     "user_decision_save",
    "detail":   "user_decision_detail",
    "navigate": "user_decision_navigate",
}


def resume_analysis(
    thread_id: str,
    decision: str,         # "yes" / "no"
    stage: str,            # "generate" / "save" / "detail" / "navigate"
) -> Dict[str, Any]:
    """HITL 결정 받아 재개."""
    state_key = _RESUME_STATE_KEY.get(stage)
    if state_key is None:
        return {"error": f"unknown stage: {stage}"}

    graph = get_graph()
    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": _GRAPH_RECURSION_LIMIT,
    }
    graph.update_state(config, {state_key: decision})

    final = graph.invoke(None, config)
    return _build_api_response(graph, config, final)


# HITL next_action 정적 매핑 (gate_navigate 는 동적이라 제외)
_HITL_NEXT_ACTION: Dict[str, Dict[str, Any]] = {
    "gate_generate": {
        "type": "hitl",
        "stage": "ask_generate",
        "question": "보고서 생성할까요?",
        "options": ["yes", "no"],
    },
    "gate_save": {
        "type": "hitl",
        "stage": "ask_save",
        "question": "내 파일함의 'AI 보고서' 폴더에 저장할까요?",
        "options": ["yes", "no"],
    },
    "gate_detail": {
        "type": "hitl",
        "stage": "ask_detail",
        "question": "더 자세히 설명해드릴까요?",
        "options": ["yes", "no"],
    },
}


def _build_api_response(graph, config, final_state) -> Dict[str, Any]:
    """final state + checkpoint state 합쳐서 응답 생성.

    invoke() 와 invoke(None) 둘 다 마지막 노드 출력만 반환 가능 →
    완전한 response 는 checkpoint snapshot 에서 가져옴.
    """
    snapshot = graph.get_state(config)
    state_values = snapshot.values if snapshot else {}

    # 우선순위: snapshot 의 누적 state → final_state (마지막 노드 출력) 폴백
    response = dict(state_values.get("response") or
                    (final_state.get("response") if final_state else {}) or
                    {})

    # 다음 인터럽트 노드 있으면 HITL pending 표시
    if snapshot and snapshot.next:
        next_node = snapshot.next[0]
        static_action = _HITL_NEXT_ACTION.get(next_node)
        if static_action is not None:
            response["next_action"] = dict(static_action)
        elif next_node == "gate_navigate":
            # 동적 — target_page 라벨 끼워서 question 생성
            target = state_values.get("target_page") or {}
            label = target.get("label", "해당")
            response["next_action"] = {
                "type": "hitl",
                "stage": "ask_navigate",
                "question": f"{label} 페이지로 이동할까요?",
                "options": ["yes", "no"],
            }
    else:
        # 그래프 종료 — HITL pending 만 제거. navigate 같은 종료 액션은 FE 가 라우팅에 써야 하므로 보존.
        na = response.get("next_action")
        if isinstance(na, dict) and na.get("type") == "hitl":
            response.pop("next_action", None)

    # 안전장치: response 가 어떤 이유로든 비어있으면 unknown 으로 처리
    if not response:
        response = {
            "intent": "unknown",
            "form": "short",
            "narrative": "응답 생성에 실패했습니다. 다시 시도해주세요.",
        }
    elif "intent" not in response:
        # intent 누락 시 보강 (FE 에서 분기 못 잡는 거 방지)
        response["intent"] = "analyze"
        response.setdefault("narrative", "응답 일부가 누락되었습니다.")

    return response
