"""LLM 프롬프트 템플릿.

LangGraph 노드들이 호출하는 모든 프롬프트를 흐름 단위로 묶어 보관.

흐름별 묶음:
- context: 모든 intent 진입 전 공통 컨텍스트 해소
- intent:  라우팅 결정 (어느 분기, 어느 도구)
- analyze: analyze 분기의 narrative 생성
- explain: explain 분기의 답변 생성 (기본 / 상세)
- crag:    explain 의 CRAG 자기수정 루프 (Judge·rewriter)
"""

from .context import RESOLVE_CONTEXT_PROMPT
from .intent import INTENT_PROMPT, TOOL_SELECT_PROMPT
from .analyze import SHORT_PROMPT
from .explain import EXPLAIN_PROMPT, EXPLAIN_PROMPT_DETAILED
from .crag import GRADE_CHUNKS_PROMPT, GRADE_ANSWER_PROMPT, REWRITE_QUERY_PROMPT

__all__ = [
    "RESOLVE_CONTEXT_PROMPT",
    "INTENT_PROMPT",
    "TOOL_SELECT_PROMPT",
    "SHORT_PROMPT",
    "EXPLAIN_PROMPT",
    "EXPLAIN_PROMPT_DETAILED",
    "GRADE_CHUNKS_PROMPT",
    "GRADE_ANSWER_PROMPT",
    "REWRITE_QUERY_PROMPT",
]
