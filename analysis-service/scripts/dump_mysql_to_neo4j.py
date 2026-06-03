"""
MySQL → Neo4j 초기 적재 — 멱등성 보장 (MERGE 패턴).

같은 데이터로 여러 번 실행해도 중복·에러 없음.
CDC 자동 동기화 깔리기 전엔 더미 추가 시 수동 재실행으로 보강.

실행:
  docker exec analysis-service python /app/scripts/dump_mysql_to_neo4j.py

그래프 모델:
  (:사원   {emp_id, name, status, role, hire_date, resign_date})
  (:부서   {dept_id, dept_name, dept_code})
  (:시즌   {season_id, name, status, start_date, end_date})
  (:등급   {grade_id, grade_code, grade_name})

  (사원)-[:소속]->(부서)
  (부서)-[:상위]->(부서)                    # 조직도
  (사원)-[:받음 {season_id, total_score}]->(등급)
  (사원)-[:평가함 {season_id}]->(사원)       # manager_evaluation
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any, Dict, Iterable, List

# /app 을 sys.path 에 추가 (스크립트 위치 /app/scripts/ → analysis 모듈 import)
sys.path.insert(0, "/app")

from neo4j import GraphDatabase, Driver
from sqlalchemy import text

from analysis.db import get_session


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("dump_to_neo4j")


# ─── 설정 ───
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://host.docker.internal:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASS = os.getenv("NEO4J_PASSWORD", "test1234")

BATCH_SIZE = 500


# ─── 헬퍼 ───
def _to_str(v: Any) -> Any:
    """datetime/date 등을 ISO 문자열로 (Neo4j 호환 + JSON 직렬화 안전)."""
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def _bin_to_hex(b: bytes) -> str | None:
    """company_id 등 BINARY(16) → UUID 문자열."""
    if b is None:
        return None
    if isinstance(b, str):
        return b
    h = b.hex()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


# ─── 인덱스/제약 ───
INDEXES = [
    "CREATE CONSTRAINT 사원_emp_id IF NOT EXISTS FOR (e:사원) REQUIRE e.emp_id IS UNIQUE",
    "CREATE CONSTRAINT 부서_dept_id IF NOT EXISTS FOR (d:부서) REQUIRE d.dept_id IS UNIQUE",
    "CREATE CONSTRAINT 시즌_season_id IF NOT EXISTS FOR (s:시즌) REQUIRE s.season_id IS UNIQUE",
    "CREATE CONSTRAINT 직급_grade_id IF NOT EXISTS FOR (g:직급) REQUIRE g.grade_id IS UNIQUE",
    "CREATE CONSTRAINT 평가등급_code IF NOT EXISTS FOR (g:평가등급) REQUIRE g.code IS UNIQUE",
]


# ─── 추출 + 적재 ───
def dump_departments(session, driver: Driver) -> int:
    rows = session.execute(text("""
        SELECT dept_id, dept_code, dept_name, parent_dept_id, sort_order
        FROM department
        WHERE is_use = 'Y'
    """)).mappings().all()

    nodes = [dict(r) for r in rows]
    with driver.session() as s:
        s.execute_write(
            lambda tx: tx.run("""
                UNWIND $rows AS row
                MERGE (d:부서 {dept_id: row.dept_id})
                SET d.dept_code = row.dept_code,
                    d.dept_name = row.dept_name,
                    d.sort_order = row.sort_order
            """, rows=nodes)
        )
        # 부서 상위 관계
        s.execute_write(
            lambda tx: tx.run("""
                UNWIND $rows AS row
                WITH row WHERE row.parent_dept_id IS NOT NULL
                MATCH (child:부서 {dept_id: row.dept_id})
                MATCH (parent:부서 {dept_id: row.parent_dept_id})
                MERGE (child)-[:상위]->(parent)
            """, rows=nodes)
        )
    logger.info(f"부서: {len(nodes)} 노드")
    return len(nodes)


def dump_seasons(session, driver: Driver) -> int:
    rows = session.execute(text("""
        SELECT season_id, name, status, start_date, end_date, period
        FROM season
    """)).mappings().all()

    nodes = [
        {
            "season_id": r["season_id"],
            "name": r["name"],
            "status": r["status"],
            "start_date": _to_str(r["start_date"]),
            "end_date": _to_str(r["end_date"]),
            "period": r["period"],
        } for r in rows
    ]
    with driver.session() as s:
        s.execute_write(
            lambda tx: tx.run("""
                UNWIND $rows AS row
                MERGE (sn:시즌 {season_id: row.season_id})
                SET sn.name = row.name,
                    sn.status = row.status,
                    sn.start_date = row.start_date,
                    sn.end_date = row.end_date,
                    sn.period = row.period
            """, rows=nodes)
        )
    logger.info(f"시즌: {len(nodes)} 노드")
    return len(nodes)


def dump_grades(session, driver: Driver) -> int:
    """직급 노드 (G1~G6) — grade 테이블."""
    rows = session.execute(text("""
        SELECT grade_id, grade_code, grade_name, grade_order
        FROM grade
    """)).mappings().all()

    nodes = [dict(r) for r in rows]
    with driver.session() as s:
        s.execute_write(
            lambda tx: tx.run("""
                UNWIND $rows AS row
                MERGE (g:직급 {grade_id: row.grade_id})
                SET g.grade_code = row.grade_code,
                    g.grade_name = row.grade_name,
                    g.grade_order = row.grade_order
            """, rows=nodes)
        )
    logger.info(f"직급: {len(nodes)} 노드")
    return len(nodes)


def dump_eval_grade_nodes(driver: Driver) -> int:
    """평가등급 노드 (S/A/B/C/D) — eval_grade.final_grade 의 값들."""
    codes = ["S", "A", "B", "C", "D"]
    with driver.session() as s:
        s.execute_write(
            lambda tx: tx.run("""
                UNWIND $codes AS code
                MERGE (g:평가등급 {code: code})
            """, codes=codes)
        )
    logger.info(f"평가등급: {len(codes)} 노드 (S/A/B/C/D)")
    return len(codes)


def dump_employees(session, driver: Driver) -> int:
    # v_active_employee 뷰 사용 — 재직중·임원제외 등 도메인 규칙 캡슐화돼있음
    rows = session.execute(text("""
        SELECT e.emp_id, e.emp_num, e.emp_name, e.emp_status, e.emp_role,
               e.emp_hire_date, e.emp_resign, e.dept_id, e.grade_id, e.title_id
        FROM employee e
        WHERE e.delete_at IS NULL
    """)).mappings().all()

    nodes = [
        {
            "emp_id": r["emp_id"],
            "emp_num": r["emp_num"],
            "name": r["emp_name"],
            "status": r["emp_status"],
            "role": r["emp_role"],
            "hire_date": _to_str(r["emp_hire_date"]),
            "resign_date": _to_str(r["emp_resign"]),
            "dept_id": r["dept_id"],
            "grade_id": r["grade_id"],
        } for r in rows
    ]
    with driver.session() as s:
        s.execute_write(
            lambda tx: tx.run("""
                UNWIND $rows AS row
                MERGE (e:사원 {emp_id: row.emp_id})
                SET e.emp_num = row.emp_num,
                    e.name = row.name,
                    e.status = row.status,
                    e.role = row.role,
                    e.hire_date = row.hire_date,
                    e.resign_date = row.resign_date
            """, rows=nodes)
        )
        # 사원 - 소속 - 부서
        s.execute_write(
            lambda tx: tx.run("""
                UNWIND $rows AS row
                WITH row WHERE row.dept_id IS NOT NULL
                MATCH (e:사원 {emp_id: row.emp_id})
                MATCH (d:부서 {dept_id: row.dept_id})
                MERGE (e)-[:소속]->(d)
            """, rows=nodes)
        )
    logger.info(f"사원: {len(nodes)} 노드 + 소속 관계")
    return len(nodes)


def dump_eval_grades(session, driver: Driver) -> int:
    """사원이 시즌별로 받은 평가등급. (사원)-[:받음 {season_id, total_score}]->(평가등급)"""
    rows = session.execute(text("""
        SELECT emp_id, season_id, final_grade, total_score, rank_in_season
        FROM eval_grade
        WHERE final_grade IS NOT NULL
    """)).mappings().all()

    edges = [
        {
            "emp_id": r["emp_id"],
            "season_id": r["season_id"],
            "code": r["final_grade"],
            "total_score": float(r["total_score"]) if r["total_score"] is not None else None,
            "rank_in_season": r["rank_in_season"],
        } for r in rows
    ]
    with driver.session() as s:
        s.execute_write(
            lambda tx: tx.run("""
                UNWIND $rows AS row
                MATCH (e:사원 {emp_id: row.emp_id})
                MATCH (g:평가등급 {code: row.code})
                MERGE (e)-[r:받음 {season_id: row.season_id}]->(g)
                SET r.total_score = row.total_score,
                    r.rank_in_season = row.rank_in_season
            """, rows=edges)
        )
    logger.info(f"받음 관계 (사원-평가등급): {len(edges)}")
    return len(edges)


def dump_evaluations(session, driver: Driver) -> int:
    """평가자 → 피평가자. (사원)-[:평가함 {season_id, grade_label}]->(사원)"""
    rows = session.execute(text("""
        SELECT evaluator_id, employee_id, season_id, grade_label
        FROM manager_evaluation
        WHERE submitted_at IS NOT NULL
    """)).mappings().all()

    edges = [
        {
            "evaluator_id": r["evaluator_id"],
            "employee_id": r["employee_id"],
            "season_id": r["season_id"],
            "grade_label": r["grade_label"],
        } for r in rows
    ]
    with driver.session() as s:
        s.execute_write(
            lambda tx: tx.run("""
                UNWIND $rows AS row
                MATCH (ev:사원 {emp_id: row.evaluator_id})
                MATCH (target:사원 {emp_id: row.employee_id})
                MERGE (ev)-[r:평가함 {season_id: row.season_id}]->(target)
                SET r.grade_label = row.grade_label
            """, rows=edges)
        )
    logger.info(f"평가함 관계 (사원→사원): {len(edges)}")
    return len(edges)


# ─── 메인 ───
def main() -> int:
    logger.info(f"Neo4j 연결: {NEO4J_URI}")
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))

    # 인덱스/제약 (멱등성)
    with driver.session() as s:
        for stmt in INDEXES:
            s.run(stmt)
    logger.info("인덱스/제약 적용 완료")

    counts: Dict[str, int] = {}
    counts["평가등급"] = dump_eval_grade_nodes(driver)
    with get_session() as s:
        counts["부서"] = dump_departments(s, driver)
        counts["시즌"] = dump_seasons(s, driver)
        counts["직급"] = dump_grades(s, driver)
        counts["사원"] = dump_employees(s, driver)
        counts["받음"] = dump_eval_grades(s, driver)
        counts["평가함"] = dump_evaluations(s, driver)

    # 결과 통계
    with driver.session() as s:
        n_nodes = s.run("MATCH (n) RETURN count(n) AS c").single()["c"]
        n_edges = s.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]

    driver.close()

    logger.info("===== 적재 완료 =====")
    for k, v in counts.items():
        logger.info(f"  {k}: {v}")
    logger.info(f"Neo4j 총 노드: {n_nodes}, 총 관계: {n_edges}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
