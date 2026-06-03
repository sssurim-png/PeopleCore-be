"""
GraphRAG 그래프 구축 — docs/ 의 md 파일을 엔티티/관계 그래프로 인덱싱.

CLI 진입점은 __main__.py.
"""
from __future__ import annotations

import glob
import logging
import os
from typing import List, Tuple

import frontmatter  # python-frontmatter (이미 requirements 에 있음)

from analysis.rag.graph.store import GraphStore


logger = logging.getLogger("analysis.rag.graph.builder")


# docs 폴더 위치 — 컨테이너 안에선 /app/analysis/rag/docs
def _docs_root() -> str:
    here = os.path.dirname(os.path.abspath(__file__))   # analysis/rag/graph/
    return os.path.normpath(os.path.join(here, "..", "docs"))


def _collect_docs() -> List[Tuple[str, str]]:
    """
    docs/ 안의 모든 md → [(파일경로, 본문 텍스트)]

    Frontmatter 가 있으면 본문만 추출. 카테고리(indicators/procedures/rules/usage) 정보는
    파일 경로에 포함돼 nano-graphrag 가 메타데이터로 활용 가능.
    """
    root = _docs_root()
    out: List[Tuple[str, str]] = []
    for path in sorted(glob.glob(os.path.join(root, "**", "*.md"), recursive=True)):
        try:
            post = frontmatter.load(path)
            content = post.content.strip()
            if content:
                out.append((path, content))
        except Exception as e:
            logger.warning(f"문서 로드 실패 {path}: {e}")
    return out


def build_graph(force_rebuild: bool = False) -> dict:
    """
    docs/ → nano-graphrag 인덱싱 실행.

    Args:
        force_rebuild: True 면 기존 working_dir 무시하고 처음부터 다시 빌드

    Returns:
        {"ok": bool, "docs_count": int, "error": str?}
    """
    store = GraphStore.get()
    rag = store.rag()
    if rag is None:
        return {"ok": False, "error": "nano-graphrag 초기화 실패"}

    if store.is_ready() and not force_rebuild:
        logger.info("그래프가 이미 색인돼 있음 — 건너뜀 (force_rebuild=True 로 재실행 가능)")
        return {"ok": True, "docs_count": 0, "skipped": True}

    docs = _collect_docs()
    if not docs:
        return {"ok": False, "error": f"docs 폴더에 md 파일 없음: {_docs_root()}"}

    logger.info(f"그래프 인덱싱 시작 — {len(docs)} 문서")
    try:
        # nano-graphrag 는 텍스트 리스트를 insert 로 받음
        texts = [t for _, t in docs]
        rag.insert(texts)
        logger.info(f"그래프 인덱싱 완료 — {len(docs)} 문서")
        return {"ok": True, "docs_count": len(docs)}
    except Exception as e:
        logger.exception("그래프 인덱싱 실패")
        return {"ok": False, "error": str(e)}
