"""
GraphRAG 검색 — 쿼리 → 그래프 traversal → 종합 답변.

nano-graphrag 의 query() 가 내부에서:
  1. 쿼리 임베딩 → 관련 노드 찾기
  2. 노드 주변 traversal (1-2 hop)
  3. 관련 청크 + 커뮤니티 요약 모음
  4. LLM 으로 종합 답변 생성

여기선 그 결과를 기존 rag_context 형식과 비슷하게 래핑.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from analysis.rag.graph.store import GraphStore


logger = logging.getLogger("analysis.rag.graph.search")


def graph_search(query: str, mode: str = "local") -> Optional[Dict[str, Any]]:
    """
    쿼리 → GraphRAG 답변.

    Args:
        query: 사용자 발화
        mode:  "local" — 특정 엔티티 주변 검색 (기본). 빠름.
               "global" — 커뮤니티 요약 활용. "전체 ..." 같은 질문에 강함. 느림.

    Returns:
        {
            "answer": str,                  # LLM 답변
            "mode": "local" | "global",
            "sources": [...],               # 참고된 청크 메타 (옵션, 라이브러리 따라)
        }
        그래프 미색인 또는 라이브러리 미설치 시 None.
    """
    store = GraphStore.get()
    if not store.is_ready():
        logger.warning("graph_search: 그래프 미색인 — None 반환")
        return None

    rag = store.rag()
    if rag is None:
        return None

    try:
        # nano-graphrag 의 query 시그니처: rag.query(query, param=QueryParam(mode=...))
        from nano_graphrag import QueryParam
        answer = rag.query(query, param=QueryParam(mode=mode))
        return {
            "answer": str(answer),
            "mode": mode,
            "sources": [],
        }
    except Exception as e:
        logger.exception(f"graph_search 실패 ({mode}): {e}")
        return None


# ─── 복합 시그널 — 일반 RAG vs GraphRAG 분기용 ───
_COMPLEX_SIGNALS = (
    "관계", "연관", "상관",
    "와의 ", "과의 ", "사이의",
    "비교", "차이", "다른",
    "전체", "흐름", "어떻게 돌아",
    "공통점", "차이점",
    "같이", "함께", "교차",
)


def is_complex_query(query: str) -> bool:
    """복합 추론이 필요한 질문인지 휴리스틱 판단."""
    q = query.lower()
    return any(sig in q for sig in _COMPLEX_SIGNALS)
