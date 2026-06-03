"""
I-13 관계 네트워크 분석 — Neo4j 기반.

기존 SQL 도구로는 어려운 다중 hop 관계·조직도·평가자 네트워크를 Cypher 로 처리.
analysis 영역 4가지 중 'DB 관계 탐색' 영역의 첫 도구.

mode 별 쿼리:
  - evaluator_network : 평가자 네트워크 (가장 많이 평가한 평가자 + 그들이 평가한 사원)
  - org_chart        : 조직도 (부서 트리 + 부서별 사원 수)
  - mutual_eval      : 상호 평가 (서로 평가하는 사원 쌍)
  - evaluator_path   : 2-hop 평가 경로 (내 평가자의 평가자)
  - dept_grade_mix   : 부서별 평가등급 분포
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from neo4j import GraphDatabase


logger = logging.getLogger("analysis.tools.relation_network")


_driver = None


def _get_driver():
    """Neo4j 드라이버 싱글톤 — graph.py 라이프사이클과 분리."""
    global _driver
    if _driver is None:
        uri = os.getenv("NEO4J_URI", "bolt://host.docker.internal:7687")
        user = os.getenv("NEO4J_USER", "neo4j")
        pw = os.getenv("NEO4J_PASSWORD", "test1234")
        try:
            _driver = GraphDatabase.driver(uri, auth=(user, pw))
            logger.info(f"Neo4j 드라이버 초기화: {uri}")
        except Exception as e:
            logger.exception(f"Neo4j 드라이버 실패: {e}")
            _driver = None
    return _driver


# ─── 쿼리 정의 ───
_QUERIES: Dict[str, Dict[str, Any]] = {
    "evaluator_network": {
        "title": "평가자 네트워크",
        "cypher": """
            MATCH (ev:사원)-[r:평가함]->(target:사원)
            WITH ev, count(DISTINCT target) AS n_eval, collect(DISTINCT target.name)[..5] AS samples
            WHERE n_eval >= 2
            RETURN ev.name AS 평가자, n_eval AS 평가대상수, samples AS 일부대상
            ORDER BY n_eval DESC
            LIMIT 10
        """,
    },
    "org_chart": {
        "title": "조직도 + 부서별 인원",
        "cypher": """
            MATCH (d:부서)
            OPTIONAL MATCH (e:사원)-[:소속]->(d)
            WITH d, count(e) AS 사원수
            OPTIONAL MATCH (d)-[:상위]->(parent:부서)
            RETURN d.dept_name AS 부서, 사원수, parent.dept_name AS 상위부서
            ORDER BY 사원수 DESC
        """,
    },
    "mutual_eval": {
        "title": "상호 평가 (서로 평가하는 쌍)",
        "cypher": """
            MATCH (a:사원)-[:평가함]->(b:사원)-[:평가함]->(a)
            WHERE a.emp_id < b.emp_id
            RETURN a.name AS 사원A, b.name AS 사원B
            LIMIT 20
        """,
    },
    "evaluator_path": {
        "title": "2-hop 평가 경로 — 평가자의 평가자",
        "cypher": """
            MATCH (target:사원)<-[:평가함]-(ev1:사원)<-[:평가함]-(ev2:사원)
            WHERE target <> ev2
            RETURN DISTINCT target.name AS 본인,
                   ev1.name AS 직접평가자,
                   ev2.name AS 평가자의평가자
            LIMIT 15
        """,
    },
    "dept_grade_mix": {
        "title": "부서별 평가등급 분포",
        "cypher": """
            MATCH (d:부서)<-[:소속]-(e:사원)-[:받음]->(g:평가등급)
            WITH d.dept_name AS 부서, g.code AS 등급, count(e) AS 인원
            RETURN 부서, 등급, 인원
            ORDER BY 부서, 등급
        """,
    },
}


# ─── 메인 함수 ───
def analyze_relation_network(
    mode: str = "evaluator_network",
    company_id: Optional[str] = None,
    season_id: Optional[int] = None,
    **_extra: Any,
) -> Dict[str, Any]:
    """
    Multi-tool Agent 가 호출하는 표준 시그니처.

    mode 가 5개 중 하나여야 함. 알 수 없으면 evaluator_network 기본.
    company_id / season_id 는 호환을 위해 받지만 현재 Neo4j 데이터엔 미사용.
    """
    if mode not in _QUERIES:
        logger.warning(f"알 수 없는 mode '{mode}' → evaluator_network 기본")
        mode = "evaluator_network"

    spec = _QUERIES[mode]
    driver = _get_driver()
    if driver is None:
        return {
            "indicator_id": "I-13",
            "report_id": "#13",
            "summary": {"mode": "ERROR", "skipped_reason": "Neo4j 드라이버 연결 실패"},
            "error": "Neo4j 미연결",
        }

    with driver.session() as s:
        result = s.run(spec["cypher"]).data()

    return {
        "indicator_id": "I-13",
        "report_id": "#13",
        "title": spec["title"],
        "summary": {
            "mode": mode,
            "n_results": len(result),
        },
        # Multi-tool reasoner 가 읽기 좋은 형태로
        "rows": result,
    }
