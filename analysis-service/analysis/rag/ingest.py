"""
RAG 인덱싱 스크립트 — 마크다운 (진단 기준 + 평가 흐름·규칙) → ES 인덱스.

청크 단위: ## (level 2) 헤더 단위
파일: analysis/rag/docs/**/*.md (모든 카테고리 자동 스캔)
  - indicators/  진단 기준 (#1~#9, #12)
  - procedures/  평가 흐름·단계
  - rules/       평가 규칙 (강제분포·점수 등)
  - usage/       UI 사용법

카테고리는 부모 폴더명으로 자동 추출 → 검색 필터에 활용.

사용:
  $ python -m analysis.rag.ingest          # 전체 재인덱싱 (drop & create)
  $ python -m analysis.rag.ingest --update # 인덱스 유지하고 upsert
"""
import os
import sys
import re
import logging
import argparse
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Any

import frontmatter
from elasticsearch import Elasticsearch
from elasticsearch.helpers import bulk

from analysis.rag.mapping import INDEX_MAPPING, get_index_name
from analysis.rag.embeddings import embed_documents


logger = logging.getLogger("analysis.rag.ingest")


# ─── 경로 ───
# 모든 카테고리 (indicators, procedures, rules, usage) 자동 포함
DOCS_DIR = Path(__file__).parent / "docs"


# ─── ES 클라이언트 ───
def get_es() -> Elasticsearch:
    es_url = os.getenv("ES_URL", "http://localhost:9200")
    return Elasticsearch(es_url)


# ─── 청크 분할 — ## 헤더 단위 ───
SECTION_RE = re.compile(r"^## (.+)$", re.MULTILINE)


def split_into_sections(markdown: str) -> List[Dict[str, Any]]:
    """
    마크다운 본문을 ## 헤더 단위 청크로 분할.

    반환: [{"section": "분석 정의", "content": "...", "order": 0}, ...]
    """
    chunks = []
    # ## 매치 위치 모음 + 본문 끝
    matches = list(SECTION_RE.finditer(markdown))
    if not matches:
        # ## 헤더 없으면 통째로 1개 청크
        return [{"section": "본문", "content": markdown.strip(), "order": 0}]

    for i, m in enumerate(matches):
        section_title = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
        content = markdown[start:end].strip()
        chunks.append({
            "section": section_title,
            "content": content,
            "order": i,
        })

    return chunks


def build_embedding_text(doc_title: str, section: str, content: str) -> str:
    """
    임베딩 입력 텍스트 — 메타 컨텍스트 포함하면 임베딩 품질 ↑.
    예: "[보상-성과 정합성 진단 기준 / 분석 정의]\n실제 본문..."
    """
    return f"[{doc_title} / {section}]\n{content}"


# ─── 마크다운 1개 → 청크 docs ───
def parse_markdown_file(path: Path) -> List[Dict[str, Any]]:
    """frontmatter + 청크 분할 → ES doc 리스트.

    카테고리 = 부모 폴더명 (indicators / procedures / rules / usage).
    indicators 만 indicator_id·report_id 필수, 나머지는 doc_id·title 만 있으면 OK.
    """
    post = frontmatter.load(path)
    fm = post.metadata
    body = post.content

    # 카테고리 = 부모 폴더명
    category = path.parent.name  # indicators / procedures / rules / usage

    title = fm.get("title")
    if not title:
        logger.warning(f"title 부족 — 스킵: {path.name}")
        return []

    # 카테고리별 ID 추출 (indicators 는 indicator_id, 그 외는 doc_id 또는 파일명)
    if category == "indicators":
        doc_id = fm.get("indicator_id")
        report_id = fm.get("report_id", "")
        if not doc_id:
            logger.warning(f"indicator_id 부족 — 스킵: {path.name}")
            return []
    else:
        doc_id = fm.get("doc_id") or path.stem  # 파일명 (확장자 제외)
        report_id = fm.get("report_id", "")

    sections = split_into_sections(body)
    docs = []
    indexed_at = datetime.now(timezone.utc).isoformat()

    for chunk in sections:
        embedding_text = build_embedding_text(
            doc_title=title,
            section=chunk["section"],
            content=chunk["content"],
        )
        docs.append({
            "category": category,
            "indicator_id": doc_id,    # 통합 — indicators 든 procedures 든 같은 필드
            "report_id": report_id,
            "doc_title": title,
            "version": str(fm.get("version", "")),
            "last_review": str(fm.get("last_review", "")) or None,
            "section": chunk["section"],
            "section_order": chunk["order"],
            "content": chunk["content"],
            "_embedding_text": embedding_text,
            "source_path": f"{category}/{path.name}",
            "indexed_at": indexed_at,
        })

    return docs


# ─── 임베딩 일괄 생성 ───
def embed_docs_batch(docs: List[Dict[str, Any]], batch_size: int = 32) -> None:
    """docs 의 _embedding_text 를 배치 임베딩 → embedding 필드 채움."""
    texts = [d["_embedding_text"] for d in docs]
    logger.info(f"임베딩 생성: {len(texts)} 건 (배치 크기 {batch_size})")

    all_vectors = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        vectors = embed_documents(batch)
        all_vectors.extend(vectors)
        logger.info(f"  진행 {min(i + batch_size, len(texts))}/{len(texts)}")

    for d, v in zip(docs, all_vectors):
        d["embedding"] = v
        del d["_embedding_text"]


# ─── ES bulk 인덱싱 ───
def to_bulk_actions(docs: List[Dict[str, Any]], index: str):
    """docs → bulk action generator. _id = indicator_id + section_order."""
    for d in docs:
        yield {
            "_index": index,
            "_id": f"{d['indicator_id']}__{d['section_order']:02d}",
            "_source": d,
        }


def index_all(drop_first: bool = True) -> None:
    """전체 마크다운 인덱싱."""
    es = get_es()
    index = get_index_name()

    # 1. 인덱스 (재)생성
    if drop_first and es.indices.exists(index=index):
        logger.info(f"기존 인덱스 삭제: {index}")
        es.indices.delete(index=index)

    if not es.indices.exists(index=index):
        logger.info(f"인덱스 생성: {index}")
        es.indices.create(index=index, **INDEX_MAPPING)

    # 2. 마크다운 → docs (모든 카테고리 하위 폴더 재귀 스캔)
    md_files = sorted(DOCS_DIR.glob("**/*.md"))
    logger.info(f"마크다운 발견: {len(md_files)} 파일 (모든 카테고리)")

    all_docs = []
    for path in md_files:
        docs = parse_markdown_file(path)
        logger.info(f"  {path.name}: {len(docs)} 청크")
        all_docs.extend(docs)

    if not all_docs:
        logger.warning("인덱싱할 청크 없음 — 종료")
        return

    # 3. 임베딩 생성
    embed_docs_batch(all_docs)

    # 4. ES bulk 인덱싱
    logger.info(f"ES bulk 인덱싱: {len(all_docs)} 건 → {index}")
    success, errors = bulk(es, to_bulk_actions(all_docs, index), raise_on_error=False)
    logger.info(f"성공 {success} / 오류 {len(errors) if isinstance(errors, list) else errors}")
    if errors and isinstance(errors, list):
        for e in errors[:3]:
            logger.error(f"  bulk 오류: {e}")

    # 5. 새로고침 → 즉시 검색 가능
    es.indices.refresh(index=index)
    count = es.count(index=index)["count"]
    logger.info(f"인덱싱 완료. 인덱스 문서 수: {count}")


# ─── CLI ───
def main():
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="RAG 진단 기준 인덱싱")
    parser.add_argument(
        "--update",
        action="store_true",
        help="인덱스 유지하고 upsert (기본은 drop & recreate)",
    )
    args = parser.parse_args()
    index_all(drop_first=not args.update)


if __name__ == "__main__":
    main()
