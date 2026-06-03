"""
Neo4j CDC Consumer — Debezium Kafka 메시지를 받아 Neo4j 그래프 갱신.

구독 토픽:
  - peoplecore.peoplecore.employee     ← 동료 커넥터
  - peoplecore.peoplecore.department   ← 동료 커넥터
  - analysis.peoplecore.eval_grade     ← 우리 커넥터
  - analysis.peoplecore.manager_evaluation
  - analysis.peoplecore.season

Debezium 메시지 형식:
  {
    "payload": {
      "before": {...},      # 변경 전 (UPDATE/DELETE 시)
      "after":  {...},      # 변경 후 (CREATE/UPDATE 시)
      "op": "c" | "u" | "d" # create / update / delete
    }
  }

동작:
  - op = c/u → Cypher MERGE (upsert, 멱등성)
  - op = d   → Cypher DETACH DELETE
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
from typing import Any, Dict, Optional

from kafka import KafkaConsumer
from neo4j import GraphDatabase


logger = logging.getLogger("analysis.cdc.neo4j_consumer")


# ─── 설정 ───
KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "kafka:29092")
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://host.docker.internal:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASS = os.getenv("NEO4J_PASSWORD", "test1234")

TOPICS = [
    "peoplecore.peoplecore.employee",
    "peoplecore.peoplecore.department",
    "analysis.peoplecore.eval_grade",
    "analysis.peoplecore.manager_evaluation",
    "analysis.peoplecore.season",
]

_stop_event = threading.Event()


# ─── 핸들러 — 테이블별 Cypher ───
def _handle_employee(driver, op: str, after: Dict, before: Dict) -> None:
    if op == "d":
        emp_id = (before or {}).get("emp_id")
        if emp_id is not None:
            with driver.session() as s:
                s.run("MATCH (e:사원 {emp_id: $id}) DETACH DELETE e", id=emp_id)
        return
    if not after:
        return
    params = {
        "emp_id": after.get("emp_id"),
        "emp_num": after.get("emp_num"),
        "name": after.get("emp_name"),
        "status": after.get("emp_status"),
        "role": after.get("emp_role"),
        "hire_date": str(after.get("emp_hire_date")) if after.get("emp_hire_date") else None,
        "dept_id": after.get("dept_id"),
    }
    with driver.session() as s:
        s.run("""
            MERGE (e:사원 {emp_id: $emp_id})
            SET e.emp_num = $emp_num,
                e.name = $name,
                e.status = $status,
                e.role = $role,
                e.hire_date = $hire_date
        """, **params)
        if params["dept_id"] is not None:
            s.run("""
                MATCH (e:사원 {emp_id: $emp_id})
                OPTIONAL MATCH (e)-[r:소속]->()
                DELETE r
                WITH e
                MATCH (d:부서 {dept_id: $dept_id})
                MERGE (e)-[:소속]->(d)
            """, emp_id=params["emp_id"], dept_id=params["dept_id"])


def _handle_department(driver, op: str, after: Dict, before: Dict) -> None:
    if op == "d":
        dept_id = (before or {}).get("dept_id")
        if dept_id is not None:
            with driver.session() as s:
                s.run("MATCH (d:부서 {dept_id: $id}) DETACH DELETE d", id=dept_id)
        return
    if not after or after.get("is_use") != "Y":
        return
    with driver.session() as s:
        s.run("""
            MERGE (d:부서 {dept_id: $dept_id})
            SET d.dept_code = $dept_code,
                d.dept_name = $dept_name,
                d.sort_order = $sort_order
        """, dept_id=after.get("dept_id"),
             dept_code=after.get("dept_code"),
             dept_name=after.get("dept_name"),
             sort_order=after.get("sort_order"))


def _handle_eval_grade(driver, op: str, after: Dict, before: Dict) -> None:
    if op == "d":
        gid = (before or {}).get("grade_id")
        if gid is not None:
            with driver.session() as s:
                s.run("""
                    MATCH (:사원)-[r:받음]->(:평가등급)
                    WHERE r.eval_grade_id = $id
                    DELETE r
                """, id=gid)
        return
    if not after or not after.get("final_grade"):
        return
    with driver.session() as s:
        s.run("""
            MERGE (g:평가등급 {code: $code})
            WITH g
            MATCH (e:사원 {emp_id: $emp_id})
            MERGE (e)-[r:받음 {season_id: $season_id}]->(g)
            SET r.total_score = $score,
                r.rank_in_season = $rank,
                r.eval_grade_id = $eg_id
        """, code=after.get("final_grade"),
             emp_id=after.get("emp_id"),
             season_id=after.get("season_id"),
             score=after.get("total_score"),
             rank=after.get("rank_in_season"),
             eg_id=after.get("grade_id"))


def _handle_manager_evaluation(driver, op: str, after: Dict, before: Dict) -> None:
    if op == "d":
        mid = (before or {}).get("mgr_eval_id")
        if mid is not None:
            with driver.session() as s:
                s.run("""
                    MATCH (:사원)-[r:평가함]->(:사원)
                    WHERE r.mgr_eval_id = $id
                    DELETE r
                """, id=mid)
        return
    if not after or after.get("submitted_at") is None:
        return
    with driver.session() as s:
        s.run("""
            MATCH (ev:사원 {emp_id: $evaluator_id})
            MATCH (target:사원 {emp_id: $employee_id})
            MERGE (ev)-[r:평가함 {season_id: $season_id}]->(target)
            SET r.grade_label = $grade_label,
                r.mgr_eval_id = $mid
        """, evaluator_id=after.get("evaluator_id"),
             employee_id=after.get("employee_id"),
             season_id=after.get("season_id"),
             grade_label=after.get("grade_label"),
             mid=after.get("mgr_eval_id"))


def _handle_season(driver, op: str, after: Dict, before: Dict) -> None:
    if op == "d":
        sid = (before or {}).get("season_id")
        if sid is not None:
            with driver.session() as s:
                s.run("MATCH (s:시즌 {season_id: $id}) DETACH DELETE s", id=sid)
        return
    if not after:
        return
    with driver.session() as s:
        s.run("""
            MERGE (sn:시즌 {season_id: $season_id})
            SET sn.name = $name,
                sn.status = $status,
                sn.start_date = $start_date,
                sn.end_date = $end_date,
                sn.period = $period
        """, season_id=after.get("season_id"),
             name=after.get("name"),
             status=after.get("status"),
             start_date=str(after.get("start_date")) if after.get("start_date") else None,
             end_date=str(after.get("end_date")) if after.get("end_date") else None,
             period=after.get("period"))


_HANDLERS = {
    "peoplecore.peoplecore.employee":           _handle_employee,
    "peoplecore.peoplecore.department":         _handle_department,
    "analysis.peoplecore.eval_grade":           _handle_eval_grade,
    "analysis.peoplecore.manager_evaluation":   _handle_manager_evaluation,
    "analysis.peoplecore.season":               _handle_season,
}


# ─── 메인 루프 ───
def run() -> None:
    logger.info(f"Neo4j 연결: {NEO4J_URI}")
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))

    logger.info(f"Kafka 구독: {KAFKA_BOOTSTRAP}, topics={TOPICS}")
    consumer = KafkaConsumer(
        *TOPICS,
        bootstrap_servers=KAFKA_BOOTSTRAP,
        group_id="neo4j-cdc-consumer",
        auto_offset_reset="earliest",
        enable_auto_commit=True,
        value_deserializer=lambda v: json.loads(v.decode("utf-8")) if v else None,
        consumer_timeout_ms=1000,   # poll 타임아웃 (stop 신호 체크 위해)
    )

    logger.info("CDC consumer 시작 — Ctrl+C 또는 SIGTERM 으로 종료")
    n_processed = 0
    while not _stop_event.is_set():
        for msg in consumer:
            if _stop_event.is_set():
                break
            try:
                _process(driver, msg.topic, msg.value)
                n_processed += 1
                if n_processed % 100 == 0:
                    logger.info(f"누적 처리: {n_processed}")
            except Exception as e:
                logger.exception(f"메시지 처리 실패 (topic={msg.topic}): {e}")

    consumer.close()
    driver.close()
    logger.info(f"CDC consumer 종료 — 총 {n_processed} 메시지 처리")


def _process(driver, topic: str, value: Optional[Dict[str, Any]]) -> None:
    if value is None:
        return
    payload = value.get("payload") if isinstance(value, dict) else None
    if not payload:
        return

    op = payload.get("op")          # c=create, u=update, d=delete, r=read(snapshot)
    if op not in ("c", "u", "d", "r"):
        return

    handler = _HANDLERS.get(topic)
    if not handler:
        return

    # snapshot(r) 은 create 처럼 처리
    effective_op = "c" if op == "r" else op
    handler(driver, effective_op, payload.get("after") or {}, payload.get("before") or {})


def _handle_signal(signum, _frame) -> None:
    logger.info(f"종료 시그널 수신 ({signum})")
    _stop_event.set()


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    try:
        run()
    except Exception:
        logger.exception("CDC consumer 비정상 종료")
        sys.exit(1)
