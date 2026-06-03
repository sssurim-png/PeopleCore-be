"""
Ollama bge-m3 임베딩 래퍼.

bge-m3:
  - 1024 차원
  - 다국어 지원 (한국어 포함)
  - Ollama 로컬 호스팅 (.env 의 OLLAMA_URL)
"""
import os
import logging
from typing import List

from langchain_ollama import OllamaEmbeddings


logger = logging.getLogger("analysis.rag.embeddings")


# ─── 싱글톤 ───
_embeddings: OllamaEmbeddings | None = None


def get_embeddings() -> OllamaEmbeddings:
    """싱글톤 임베딩 클라이언트 반환. 첫 호출 시 .env 값으로 초기화."""
    global _embeddings
    if _embeddings is None:
        ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434")
        model = os.getenv("EMBEDDING_MODEL", "bge-m3")
        logger.info(f"임베딩 초기화: model={model}, url={ollama_url}")
        _embeddings = OllamaEmbeddings(
            model=model,
            base_url=ollama_url,
        )
    return _embeddings


def embed_query(text: str) -> List[float]:
    """단일 검색 쿼리 임베딩 (검색 시 사용)."""
    return get_embeddings().embed_query(text)


def embed_documents(texts: List[str]) -> List[List[float]]:
    """다수 문서 임베딩 (인덱싱 시 사용, 배치 처리)."""
    return get_embeddings().embed_documents(texts)
