"""
GraphStore — nano-graphrag 디렉토리를 래핑.

nano-graphrag 는 자체 캐시 디렉토리(working_dir)에 그래프·임베딩·청크를 저장한다.
이 모듈은 그 디렉토리 위치를 환경변수로 관리하고 인스턴스 싱글톤을 제공.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger("analysis.rag.graph.store")


# 그래프 데이터 저장 경로 — 환경변수 GRAPH_DATA_DIR (없으면 ./graph_data)
DEFAULT_GRAPH_DIR = "/app/graph_data"


def get_graph_dir() -> str:
    return os.getenv("GRAPH_DATA_DIR", DEFAULT_GRAPH_DIR)


class GraphStore:
    """nano-graphrag GraphRAG 인스턴스 싱글톤 래퍼."""

    _instance: Optional["GraphStore"] = None

    def __init__(self) -> None:
        self._rag = None
        self._init_tried = False

    @classmethod
    def get(cls) -> "GraphStore":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def rag(self):
        """nano-graphrag GraphRAG 인스턴스 반환. 첫 호출 시 생성.
        모델: 비용·한도 절약 위해 두 단계 모두 gpt-4o-mini 명시.
        """
        if self._rag is not None or self._init_tried:
            return self._rag
        self._init_tried = True

        try:
            from nano_graphrag import GraphRAG
            from nano_graphrag._llm import gpt_4o_mini_complete
        except ImportError:
            logger.warning("nano-graphrag 미설치 — GraphRAG 비활성")
            return None

        graph_dir = get_graph_dir()
        os.makedirs(graph_dir, exist_ok=True)

        try:
            self._rag = GraphRAG(
                working_dir=graph_dir,
                enable_llm_cache=True,
                # 기본 gpt-4o 는 rate limit·비용 부담. 인덱싱·검색 모두 mini 로 통일.
                best_model_func=gpt_4o_mini_complete,
                cheap_model_func=gpt_4o_mini_complete,
                best_model_max_async=4,    # 동시 호출 제한 — rate limit 회피
                cheap_model_max_async=4,
            )
            logger.info(f"GraphRAG 초기화: {graph_dir} (gpt-4o-mini)")
        except Exception as e:
            logger.exception(f"GraphRAG 초기화 실패: {e}")
            self._rag = None

        return self._rag

    def is_ready(self) -> bool:
        """그래프가 색인돼 있는지 (working_dir 에 데이터 파일 존재 여부)."""
        graph_dir = get_graph_dir()
        if not os.path.isdir(graph_dir):
            return False
        # nano-graphrag 가 생성하는 핵심 파일 중 하나라도 있으면 색인됨으로 간주
        markers = (
            "graph_chunk_entity_relation.graphml",
            "kv_store_text_chunks.json",
        )
        return any(os.path.exists(os.path.join(graph_dir, m)) for m in markers)
