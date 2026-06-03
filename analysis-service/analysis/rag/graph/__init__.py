"""
GraphRAG — 문서를 엔티티·관계 그래프로 만들어 복합 추론 가능하게 함.

기존 RAG (BM25+벡터+RRF) 는 단일 청크 기반이라 "관계/비교/요약" 질문에 약함.
GraphRAG 는 docs/ 마크다운에서 엔티티·관계를 LLM 으로 추출해 그래프 구축.

구조:
  builder.py    — 그래프 구축 (인덱싱, 1회)
  store.py      — 그래프 저장/로드 (NetworkX or nano-graphrag dir)
  search.py     — 검색 (노드 매칭 + traversal + 답변 생성)
  prompts.py    — 한국어 엔티티/관계/요약 프롬프트
  __main__.py   — 인덱싱 CLI

사용:
  # 인덱싱 (1회)
  $ python -m analysis.rag.graph build

  # 검색 (graph.py 의 rag_search_node 가 자동 호출)
"""
from analysis.rag.graph.search import graph_search
from analysis.rag.graph.store import GraphStore

__all__ = ["graph_search", "GraphStore"]
