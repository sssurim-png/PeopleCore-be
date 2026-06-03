"""
호스트에서 직접 실행 진입점.
IntelliJ/PyCharm: 이 파일 우클릭 → Run 'analysis' 만으로 서버 시작.
터미널: cd analysis-service && python -m analysis

도커에선 사용 안 됨 (Dockerfile 의 CMD 가 uvicorn 직접 호출).
"""
import uvicorn
from dotenv import load_dotenv

# .env 먼저 로드 (uvicorn 임포트 전에)
load_dotenv()


if __name__ == "__main__":
    uvicorn.run(
        "analysis.api:app",
        host="0.0.0.0",
        port=8000,
        reload=True,  # 코드 저장 시 자동 재시작
    )
