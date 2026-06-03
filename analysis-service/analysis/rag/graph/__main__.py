"""
인덱싱 CLI.

사용:
  $ python -m analysis.rag.graph build           # 처음 빌드 (이미 색인돼있으면 스킵)
  $ python -m analysis.rag.graph build --force   # 강제 재빌드

도커 안에서:
  $ docker exec analysis-service python -m analysis.rag.graph build
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from analysis.rag.graph.builder import build_graph
from analysis.rag.graph.store import GraphStore, get_graph_dir


def main() -> int:
    parser = argparse.ArgumentParser(prog="analysis.rag.graph")
    parser.add_argument("command", choices=["build", "status"])
    parser.add_argument("--force", action="store_true", help="기존 데이터 무시하고 재빌드")
    args = parser.parse_args()

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.command == "status":
        store = GraphStore.get()
        ready = store.is_ready()
        print(f"GraphRAG 디렉토리: {get_graph_dir()}")
        print(f"색인 완료: {'YES' if ready else 'NO'}")
        return 0

    if args.command == "build":
        print(f"GraphRAG 인덱싱 시작 (force={args.force})")
        result = build_graph(force_rebuild=args.force)
        if result.get("ok"):
            if result.get("skipped"):
                print("이미 색인돼 있음 — 스킵 (--force 로 재빌드)")
            else:
                print(f"✅ 완료: {result.get('docs_count')} 문서")
            return 0
        else:
            print(f"❌ 실패: {result.get('error')}")
            return 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
