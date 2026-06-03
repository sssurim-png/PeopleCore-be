# PeopleCore HR 분석 AI 컨테이너 이미지
# 다른 팀원은 Docker 만 깔면 이 이미지로 실행 가능
# (Python·pip·라이브러리 신경 X)

# ─── 1. 베이스 이미지 — Python 3.11 (가벼운 slim 버전) ───
FROM python:3.11-slim

# ─── 2. 작업 디렉토리 ───
WORKDIR /app

# ─── 3. OS 패키지 (필요 시) ───
# 한국어 처리·빌드 도구 (numpy 같은 일부 패키지가 필요로 함)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ─── 4. Python 의존성 설치 (캐싱 최적화) ───
# requirements.txt 먼저 복사 → 의존성 안 바뀌면 캐시 재사용
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ─── 4-1. 추가 — OpenAI 클라우드 LLM (explain·resolve_context 빠른 응답용) ───
# 키 없으면 graph.py 가 자동으로 EXAONE 로 폴백하므로 안전.
RUN pip install --no-cache-dir langchain-openai==0.2.5

# ─── 5. 코드 복사 ───
COPY analysis ./analysis

# ─── 6. 환경변수 ───
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# ─── 7. 포트 노출 ───
EXPOSE 8000

# ─── 8. 헬스 체크 (Docker 가 컨테이너 상태 확인) ───
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# ─── 9. 실행 명령 ───
CMD ["uvicorn", "analysis.api:app", "--host", "0.0.0.0", "--port", "8000"]
