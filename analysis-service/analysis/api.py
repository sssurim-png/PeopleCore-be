"""
PeopleCore HR 분석 AI — FastAPI 진입점.

엔드포인트:
  GET  /health                       헬스체크
  POST /analyze                      자연어 발화 → 그래프 실행 (HITL 시 thread_id 반환)
  POST /analyze/resume               HITL 결정 받아 재개
  POST /tool/{indicator_id}          분석 도구 직접 호출 (디버깅)
  GET  /tools                        도구 목록
"""
import os
import uuid
import logging

from dotenv import load_dotenv
# 호스트에서 직접 uvicorn 띄울 때 .env 자동 로드 (도커에선 env_file 로 이미 주입돼 영향 없음)
load_dotenv()

import asyncio
import json as _json

from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, AsyncGenerator

from analysis.graph import run_analysis, resume_analysis
from analysis.tools import TOOL_REGISTRY


# ─── 로거 ───
log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("analysis")


# ─── FastAPI 앱 ───
app = FastAPI(
    title="PeopleCore HR 분석 AI",
    description="조직 단위 HR 분석 리포트 + HITL (HR_ADMIN 전용)",
    version="0.3.0",
)


# ─── CORS ───
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── 모델 ───
class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    config: dict


class AnalyzeRequest(BaseModel):
    query: str = Field(..., description="사용자 자연어 발화", min_length=1)
    thread_id: Optional[str] = Field(None, description="기존 세션 재사용 시 (없으면 생성)")


class AnalyzeResumeRequest(BaseModel):
    thread_id: str = Field(..., description="첫 호출에서 받은 thread_id")
    decision: str = Field(..., description="yes / no")
    stage: str = Field(..., description="generate / save / detail / navigate")


class ToolCallRequest(BaseModel):
    params: Dict[str, Any] = Field(default_factory=dict)


# ─── 헬스체크 ───
@app.get("/health", response_model=HealthResponse)
async def health():
    return HealthResponse(
        status="ok",
        service="analysis-service",
        version=app.version,
        config={
            "ollama_url": os.getenv("OLLAMA_URL", "not set"),
            "llm_model": os.getenv("LLM_MODEL", "not set"),
            "embedding_model": os.getenv("EMBEDDING_MODEL", "not set"),
            "es_url": os.getenv("ES_URL", "not set"),
            "es_index_prefix": os.getenv("ES_INDEX_PREFIX", "not set"),
            "db_url": os.getenv("DB_URL", "not set").split("@")[-1],
            "peoplecore_base_url": os.getenv("PEOPLECORE_BASE_URL", "not set"),
            "tools_loaded": list(TOOL_REGISTRY.keys()),
        },
    )


# ─── 루트 ───
@app.get("/")
async def root():
    return {
        "service": "analysis-service",
        "message": "PeopleCore HR 분석 AI is running. See /docs for API.",
        "tools": list(TOOL_REGISTRY.keys()),
    }


# ─── 분석 — 첫 호출 ───
@app.post("/analyze")
async def analyze(
    req: AnalyzeRequest,
    x_user_company: Optional[str] = Header(None, description="회사 UUID"),
    x_user_id: Optional[int] = Header(None, description="HR 사용자 emp_id"),
):
    """
    자연어 발화 → 분석/설명 응답.

    - short 응답: 즉시 narrative 반환
    - report 응답: chart_specs + raw_result + next_action(HITL) + thread_id 반환
    - 사용자 의도가 보고서 저장이면 /analyze/resume 으로 결정 전달

    헤더:
      X-User-Company   회사 UUID (보고서 저장 시 필요)
      X-User-Id        HR 사용자 emp_id (보고서 저장 시 필요)
    """
    thread_id = req.thread_id or f"sess_{uuid.uuid4().hex[:12]}"
    try:
        logger.info(f"/analyze [{thread_id}] 호출: {req.query[:80]}")
        result = run_analysis(
            user_query=req.query,
            thread_id=thread_id,
            auth_company_id=x_user_company,
            auth_user_emp_id=x_user_id,
        )
        result["thread_id"] = thread_id
        return result
    except Exception as e:
        logger.exception("그래프 실행 실패")
        raise HTTPException(status_code=500, detail=f"분석 실패: {e}")


# ─── 분석 — 스트리밍 (SSE) ───
@app.post("/analyze/stream")
async def analyze_stream(
    req: AnalyzeRequest,
    x_user_company: Optional[str] = Header(None),
    x_user_id: Optional[int] = Header(None),
):
    """
    Server-Sent Events 로 분석 진행 상황 + 결과 토큰 단위 전송.

    이벤트 타입:
      - event: status   data: {"stage": "...", "message": "..."}
      - event: token    data: {"text": "...chunk..."}
      - event: result   data: {<AnalyzeResponse>}      # 최종 전체 응답
      - event: error    data: {"detail": "..."}

    현재 구현: 분석은 동기로 진행하되, narrative 를 토큰 단위로 쪼개 전송 (체감 속도 ↑).
    완전한 LLM 스트리밍은 LangGraph 그래프를 async 로 재설계해야 가능 — 다음 단계.
    """
    thread_id = req.thread_id or f"sess_{uuid.uuid4().hex[:12]}"

    async def event_stream() -> AsyncGenerator[str, None]:
        def sse(event: str, data: dict) -> str:
            return f"event: {event}\ndata: {_json.dumps(data, ensure_ascii=False)}\n\n"

        try:
            yield sse("status", {"stage": "start", "message": "분석 시작"})
            yield sse("status", {"stage": "thinking", "message": "도구 선택·실행 중..."})

            # 동기 분석을 asyncio.to_thread 로 비차단 실행
            result = await asyncio.to_thread(
                run_analysis,
                req.query,
                thread_id,
                x_user_company,
                x_user_id,
            )
            result["thread_id"] = thread_id

            yield sse("status", {"stage": "rendering", "message": "응답 전송 중..."})

            # narrative 를 ~30자 단위 청크로 쪼개 토큰 이벤트 전송 (점진 렌더링 효과)
            narrative = result.get("narrative") or ""
            chunk_size = 30
            for i in range(0, len(narrative), chunk_size):
                yield sse("token", {"text": narrative[i:i + chunk_size]})
                await asyncio.sleep(0.03)   # 체감 스트리밍 + 클라이언트 부담 완화

            # 마지막에 전체 응답 ( raw_result, next_action, chart_specs 등 )
            yield sse("result", result)
        except Exception as e:
            logger.exception("스트림 실행 실패")
            yield sse("error", {"detail": str(e)})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # nginx 환경 대비
        },
    )


# ─── 분석 — HITL 재개 ───
@app.post("/analyze/resume")
async def resume(req: AnalyzeResumeRequest):
    """
    HITL 결정 받아 그래프 재개.

    stage:
      "generate" — "보고서 문서를 생성할까요?" 의 응답 (analyze 분기)
      "save"     — "내 파일함에 저장할까요?" 의 응답 (analyze 분기)
      "detail"   — "더 자세히 설명해드릴까요?" 의 응답 (explain 분기)
      "navigate" — "○○ 페이지로 이동할까요?" 의 응답 (navigate 분기)

    decision:
      "yes" / "no"
    """
    if req.decision not in ("yes", "no"):
        raise HTTPException(status_code=400, detail="decision 은 'yes' 또는 'no'")
    if req.stage not in ("generate", "save", "detail", "navigate"):
        raise HTTPException(status_code=400, detail="stage 는 'generate', 'save', 'detail', 'navigate' 중 하나")

    try:
        logger.info(f"/analyze/resume [{req.thread_id}] stage={req.stage} decision={req.decision}")
        result = resume_analysis(req.thread_id, req.decision, req.stage)
        result["thread_id"] = req.thread_id
        return result
    except Exception as e:
        logger.exception("재개 실패")
        raise HTTPException(status_code=500, detail=f"재개 실패: {e}")


# ─── 도구 직접 호출 (디버깅) ───
@app.post("/tool/{indicator_id}")
async def call_tool(indicator_id: str, req: ToolCallRequest):
    if indicator_id not in TOOL_REGISTRY:
        raise HTTPException(status_code=404, detail=f"도구 미등록: {indicator_id}")

    tool_meta = TOOL_REGISTRY[indicator_id]
    try:
        logger.info(f"/tool/{indicator_id} 직접 호출: {req.params}")
        result = tool_meta["func"](**req.params)
        return result
    except Exception as e:
        logger.exception(f"도구 실행 실패: {indicator_id}")
        raise HTTPException(status_code=500, detail=f"도구 실행 오류: {e}")


@app.get("/tools")
async def list_tools():
    return {
        tid: {
            "name": meta["name"],
            "title": meta["title"],
            "report_id": meta["report_id"],
        }
        for tid, meta in TOOL_REGISTRY.items()
    }


# ─── 시작 / 종료 이벤트 ───
@app.on_event("startup")
async def on_startup():
    logger.info("=" * 60)
    logger.info("PeopleCore HR 분석 AI 시작")
    logger.info(f"  LLM Model:       {os.getenv('LLM_MODEL', '?')}")
    logger.info(f"  Embedding Model: {os.getenv('EMBEDDING_MODEL', '?')}")
    logger.info(f"  Ollama URL:      {os.getenv('OLLAMA_URL', '?')}")
    logger.info(f"  ES URL:          {os.getenv('ES_URL', '?')}")
    logger.info(f"  DB URL:          {os.getenv('DB_URL', '?').split('@')[-1]}")
    logger.info(f"  hr-service URL:  {os.getenv('PEOPLECORE_BASE_URL', '?')}")
    logger.info(f"  Tools loaded:    {len(TOOL_REGISTRY)} ({list(TOOL_REGISTRY.keys())})")
    logger.info("=" * 60)


@app.on_event("shutdown")
async def on_shutdown():
    from analysis.db import close_engine
    close_engine()
    logger.info("PeopleCore HR 분석 AI 종료")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "analysis.api:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )
