"""
PeopleCore 백엔드(api-gateway 경유) 호출 클라이언트.

엔드포인트:
  POST /collaboration-service/internal/filevault/upload-to-personal-subfolder
       — HITL 통과 후 사용자의 PERSONAL 파일함 "AI 보고서" 자식 폴더에 마크다운 저장.
"""
from __future__ import annotations

import os
import logging
from typing import Dict, Any
import httpx


logger = logging.getLogger("analysis.hr_client")


PEOPLECORE_BASE_URL = os.getenv("PEOPLECORE_BASE_URL", "http://localhost:8080")
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "")


def upload_ai_report(
    company_id: str,
    user_emp_id: int,
    file_bytes: bytes,
    file_name: str,
    title: str,
    subfolder_name: str = "AI 보고서",
    content_type: str = "text/markdown",
    timeout: float = 30.0,
) -> Dict[str, Any]:
    """
    분석 보고서를 사용자 PERSONAL 파일함 하위 "AI 보고서" 폴더에 저장.

    게이트웨이의 internal 경로(`/collaboration-service/internal/...`)는 JWT 가 아닌
    `X-Internal-Api-Key` 로 인증되며, 사용자 식별은 `X-User-Company`/`X-User-Id` 헤더로 직접 전달.

    Returns:
        {"file_id": <Long>, "folder_id": <Long>, "folder_name": str, "file_name": str}  성공
        {"error": "..."}                                                                 실패
    """
    url = f"{PEOPLECORE_BASE_URL}/collaboration-service/internal/filevault/upload-to-personal-subfolder"
    headers = {
        "X-Internal-Api-Key": INTERNAL_API_KEY,
        "X-User-Company": company_id,
        "X-User-Id": str(user_emp_id),
    }
    files = {
        "file": (file_name, file_bytes, content_type),
    }
    data: Dict[str, Any] = {
        "subfolderName": subfolder_name,
        "displayName": title,
    }

    logger.info(f"AI 보고서 업로드: title='{title}', empId={user_emp_id}, file={file_name}")
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, headers=headers, data=data, files=files)
        if resp.status_code == 200:
            payload = resp.json()
            logger.info(f"AI 보고서 업로드 성공: {payload}")
            return {
                "file_id": payload.get("fileId"),
                "folder_id": payload.get("folderId"),
                "folder_name": payload.get("folderName"),
                "file_name": payload.get("fileName"),
            }
        logger.error(f"업로드 실패: {resp.status_code} {resp.text}")
        return {"error": f"upload failed ({resp.status_code}): {resp.text}"}
    except Exception as e:
        logger.exception("collaboration-service 호출 실패")
        return {"error": f"collaboration-service unreachable: {e}"}
