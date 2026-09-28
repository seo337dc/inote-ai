from __future__ import annotations

import json
import logging
import os
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from groq import Groq
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(tags=["book-info"])

SYSTEM_PROMPT = (
    "너는 책 정보를 알려주는 도우미야. 사용자가 준 책 제목(과 저자)을 보고, 실제로 아는 책이면 "
    "그 정보를, 모르면 제목/저자로 미루어 짐작한 일반적인 설명으로 아래 JSON 형식에 맞춰 답해:\n"
    '{"genre": "장르(한 단어~짧은 구)", "synopsis": "줄거리 2~3문장", "authorBio": "작가 소개 1~2문장"}\n'
    "genre/synopsis/authorBio 값은 반드시 한국어로 작성해 (원서가 외국 도서여도 한국어로 설명). "
    "JSON 객체 하나만 답하고, 그 외의 다른 텍스트는 절대 붙이지 마."
)

MODEL = "openai/gpt-oss-20b"


class BookInfoRequest(BaseModel):
    title: str
    author: Optional[str] = None


@router.post("/book-info")
async def generate_book_info(data: BookInfoRequest, x_internal_secret: str = Header(default="")):
    """책 제목(+저자)만 보고 장르/줄거리/작가소개를 LLM이 자동 생성 — inote-server가
    독서 기록 발행 시 호출. /summarize와 동일하게 내부 전용(x-internal-secret)."""
    if x_internal_secret != os.environ.get("INTERNAL_SECRET"):
        raise HTTPException(status_code=401, detail="unauthorized")

    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    user_content = f"제목: {data.title}"
    if data.author:
        user_content += f"\n저자: {data.author}"

    # gpt-oss 계열은 답 전에 내부적으로 "생각하는" 토큰도 max_tokens에 포함돼서,
    # 512로는 JSON을 다 못 쓰고 잘리는 경우가 실제로 있었음(json_validate_failed) — 여유있게 잡음.
    # 그래도 실패하면(모델 장애 등) 500으로 그냥 죽이지 않고 빈 필드로 응답 —
    # 이 정보는 "있으면 좋은" 보조 정보라 실패해도 독서 기록 저장 자체를 막으면 안 됨.
    try:
        completion = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            max_tokens=1536,
            response_format={"type": "json_object"},
        )
        text = completion.choices[0].message.content or "{}"
        parsed = json.loads(text)
    except Exception:
        logger.exception("failed to generate book info (title=%s)", data.title)
        parsed = {}

    return {
        "genre": parsed.get("genre"),
        "synopsis": parsed.get("synopsis"),
        "authorBio": parsed.get("authorBio"),
    }
