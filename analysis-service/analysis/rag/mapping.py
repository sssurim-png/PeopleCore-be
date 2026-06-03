"""
ES 인덱스 매핑 정의 — RAG 진단 기준 마크다운 검색용.

청크 단위: ## (level 2) 헤더 단위
하이브리드 검색: dense_vector(bge-m3, 1024) + BM25(Nori)
"""
import os

# 인덱스 이름 — analysis-indicators
INDEX_NAME = f"{os.getenv('ES_INDEX_PREFIX', 'analysis-')}indicators"

# bge-m3 임베딩 차원
EMBEDDING_DIM = 1024


# ─── Nori 한국어 분석기 + dense_vector 매핑 ───
INDEX_MAPPING = {
    "settings": {
        "analysis": {
            "tokenizer": {
                "nori_user_dict": {
                    "type": "nori_tokenizer",
                    "decompound_mode": "mixed",
                },
            },
            "analyzer": {
                "korean": {
                    "type": "custom",
                    "tokenizer": "nori_user_dict",
                    "filter": [
                        "nori_part_of_speech",
                        "lowercase",
                        "nori_readingform",
                    ],
                },
            },
        },
    },
    "mappings": {
        "properties": {
            # 메타 — 출처 식별
            "category": {"type": "keyword"},               # indicators / procedures / rules / usage
            "indicator_id": {"type": "keyword"},          # I-01, I-02, ... 또는 doc_id
            "report_id": {"type": "keyword"},              # #1, #2, ...
            "doc_title": {                                  # 문서 제목 (frontmatter title)
                "type": "text",
                "analyzer": "korean",
                "fields": {"keyword": {"type": "keyword"}},
            },
            "version": {"type": "keyword"},
            "last_review": {"type": "date"},

            # 청크 — 섹션 단위
            "section": {                                    # ## 헤더 텍스트
                "type": "text",
                "analyzer": "korean",
                "fields": {"keyword": {"type": "keyword"}},
            },
            "section_order": {"type": "integer"},          # 파일 내 섹션 순서

            # 검색 대상 본문
            "content": {                                    # 섹션 본문 (마크다운 그대로)
                "type": "text",
                "analyzer": "korean",
            },

            # 임베딩 — 본문 + 메타 컨텍스트로 생성
            "embedding": {
                "type": "dense_vector",
                "dims": EMBEDDING_DIM,
                "index": True,
                "similarity": "cosine",
            },

            # 원본 파일 추적
            "source_path": {"type": "keyword"},
            "indexed_at": {"type": "date"},
        },
    },
}


def get_index_name() -> str:
    """현재 환경의 인덱스 이름 반환 (.env 의 ES_INDEX_PREFIX 반영)."""
    return INDEX_NAME
