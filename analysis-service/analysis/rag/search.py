"""
RRF 하이브리드 검색 — BM25(Nori 한국어) + dense_vector(bge-m3) 결과 합산.

호출 흐름:
  1. 쿼리 텍스트 → bge-m3 임베딩
  2. ES 에 두 검색 동시 실행:
     - BM25 (content + section + doc_title 멀티매치)
     - kNN (embedding 코사인 유사도)
  3. RRF 점수로 합쳐서 최종 순위 결정
"""
import os
import logging
from typing import List, Dict, Any, Optional

from elasticsearch import Elasticsearch

from analysis.rag.mapping import get_index_name
from analysis.rag.embeddings import embed_query


logger = logging.getLogger("analysis.rag.search")


# ─── 기본 매개변수 ───
DEFAULT_K = 5                  # 최종 반환 청크 수
DEFAULT_RRF_K = 60             # RRF 상수 (관행적 60)
DEFAULT_BM25_TOP = 20          # BM25 단계 상위 N
DEFAULT_KNN_TOP = 20           # kNN 단계 상위 N


# ─── ES 클라이언트 ───
def get_es() -> Elasticsearch:
    es_url = os.getenv("ES_URL", "http://localhost:9200")
    return Elasticsearch(es_url)


# ─── BM25 검색 ───
def bm25_search(
    es: Elasticsearch,
    query: str,
    top_n: int,
    index: str,
    category_filter: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """content/section/doc_title 멀티 필드 BM25. category 필터 옵션."""
    base_query = {
        "multi_match": {
            "query": query,
            "fields": ["content^2", "section^1.5", "doc_title^1"],
            "type": "best_fields",
        },
    }
    if category_filter:
        base_query = {
            "bool": {
                "must": [base_query],
                "filter": [{"terms": {"category": category_filter}}],
            }
        }
    body = {
        "size": top_n,
        "query": base_query,
        "_source": [
            "indicator_id", "report_id", "doc_title",
            "section", "section_order", "content", "category",
        ],
    }
    res = es.search(index=index, body=body)
    return res["hits"]["hits"]


# ─── kNN (벡터) 검색 ───
def knn_search(
    es: Elasticsearch,
    query: str,
    top_n: int,
    index: str,
    category_filter: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """bge-m3 임베딩 → ES knn (코사인 유사도). category 필터 옵션."""
    vector = embed_query(query)
    knn_body: Dict[str, Any] = {
        "field": "embedding",
        "query_vector": vector,
        "k": top_n,
        "num_candidates": top_n * 5,        # ES 권고: k 의 5~10배
    }
    if category_filter:
        knn_body["filter"] = {"terms": {"category": category_filter}}
    body = {
        "size": top_n,
        "knn": knn_body,
        "_source": [
            "indicator_id", "report_id", "doc_title",
            "section", "section_order", "content", "category",
        ],
    }
    res = es.search(index=index, body=body)
    return res["hits"]["hits"]


# ─── RRF 점수 합산 ───
def rrf_fuse(
    bm25_hits: List[Dict[str, Any]],
    knn_hits: List[Dict[str, Any]],
    k: int = DEFAULT_RRF_K,
) -> List[Dict[str, Any]]:
    """
    두 결과를 RRF 로 합쳐 점수순 정렬.

    RRF score = Σ 1 / (k + rank)
        - rank = 1-base 순위 (1위가 가장 좋음)
        - k = 60 (관행). 작을수록 상위 결과에 가중치 큼

    문서 식별: _id (indicator_id + section_order). 같은 _id 가 양쪽에 있으면 합산.
    """
    scores: Dict[str, float] = {}
    docs: Dict[str, Dict[str, Any]] = {}

    for rank, hit in enumerate(bm25_hits, start=1):
        _id = hit["_id"]
        scores[_id] = scores.get(_id, 0.0) + 1.0 / (k + rank)
        docs[_id] = hit["_source"]

    for rank, hit in enumerate(knn_hits, start=1):
        _id = hit["_id"]
        scores[_id] = scores.get(_id, 0.0) + 1.0 / (k + rank)
        if _id not in docs:
            docs[_id] = hit["_source"]

    # 점수 내림차순 정렬
    sorted_ids = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)
    return [
        {**docs[_id], "_id": _id, "_rrf_score": scores[_id]}
        for _id in sorted_ids
    ]


# ─── 공개 API ───
def search(
    query: str,
    top_k: int = DEFAULT_K,
    bm25_top: int = DEFAULT_BM25_TOP,
    knn_top: int = DEFAULT_KNN_TOP,
    rrf_k: int = DEFAULT_RRF_K,
    indicator_filter: Optional[List[str]] = None,
    category_filter: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    하이브리드 검색 (BM25 + 벡터) → RRF 합산 → 상위 top_k 반환.

    Args:
        query: 검색 쿼리 (한국어)
        top_k: 최종 반환 청크 수
        bm25_top: BM25 단계 후보 수
        knn_top: 벡터 단계 후보 수
        rrf_k: RRF 상수 (작을수록 상위 강조)
        indicator_filter: 특정 indicator_id 만 검색 (예: ["I-01", "I-05"])
        category_filter: 특정 category 만 검색 — indicators/procedures/rules/usage
            "규칙·운영 설명" 발화는 ["rules", "usage", "procedures"] 로 좁혀
            분석 도구 내부 정의(indicators) 가 끼어들지 않게.

    Returns:
        [
          {
            "indicator_id": "I-02",
            "report_id": "#2",
            "doc_title": "우수인재 보상 누락 발굴 기준",
            "section": "발굴 정의",
            "section_order": 0,
            "content": "...",
            "category": "indicators",
            "_id": "I-02__00",
            "_rrf_score": 0.0325,
          },
          ...
        ]
    """
    es = get_es()
    index = get_index_name()

    bm25_hits = bm25_search(es, query, bm25_top, index, category_filter=category_filter)
    knn_hits = knn_search(es, query, knn_top, index, category_filter=category_filter)

    if indicator_filter:
        bm25_hits = [h for h in bm25_hits if h["_source"].get("indicator_id") in indicator_filter]
        knn_hits = [h for h in knn_hits if h["_source"].get("indicator_id") in indicator_filter]

    fused = rrf_fuse(bm25_hits, knn_hits, k=rrf_k)
    return fused[:top_k]


# ─── CLI 디버깅 ───
def _cli():
    import sys

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if len(sys.argv) < 2:
        print("사용: python -m analysis.rag.search <쿼리>")
        print("예:  python -m analysis.rag.search '우수인재 보상 누락 기준이 뭐야'")
        sys.exit(1)

    query = " ".join(sys.argv[1:])
    results = search(query, top_k=5)

    print(f"\n=== 쿼리: {query} ===\n")
    for i, r in enumerate(results, 1):
        print(f"[{i}] {r['report_id']} {r['doc_title']} / {r['section']} (RRF={r['_rrf_score']:.4f})")
        preview = r["content"][:200].replace("\n", " ")
        print(f"     {preview}...\n")


if __name__ == "__main__":
    _cli()
