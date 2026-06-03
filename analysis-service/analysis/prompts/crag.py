"""CRAG (Corrective RAG) 자기수정 루프 프롬프트.

explain 분기에 얹은 retrieve → grade → (필요 시) rewrite → retrieve 사이클의
Judge·rewriter 용 LLM 호출에서 사용. 답변 생성용 (`explain.py`) 과 분리된 이유는
역할이 다르고 (Judge·재작성 vs 답변 생성) 향후 다른 모델을 쓰게 될 가능성이 높기 때문.

무한 루프 방지는 그래프 측에서 `_MAX_REWRITES` 로 처리.
"""

GRADE_CHUNKS_PROMPT = """검색된 문서 청크가 질문에 답하기에 충분한 정보를 담고 있는지 판단하세요.
yes 또는 no 한 단어로만 답하세요.

질문: "{query}"

검색 청크:
{context}

판단 (yes/no):"""


GRADE_ANSWER_PROMPT = """답변 품질을 판단하세요. 다음 두 조건이 모두 만족되면 yes:
1. 답변이 질문에 직접 답함 (회피·"정보 없음" 류 답변이면 no)
2. 답변 내용이 청크에 근거함 (할루시네이션이면 no)

질문: "{query}"

청크:
{context}

답변: "{answer}"

판단 (yes/no):"""


REWRITE_QUERY_PROMPT = """이전 검색에서 좋은 결과를 못 찾았습니다. 같은 의도지만 다른 표현·용어로 한 줄 재작성하세요.
- 동의어·전문용어 활용 (예: "강제분포" → "등급 비율 강제 규칙")
- 불필요한 어미·서론 제거, 핵심 명사구 위주
- 한 줄만 출력 (설명 X)

원래 질문: "{query}"

재작성:"""
