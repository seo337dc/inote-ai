from __future__ import annotations

import os
import re

from fastapi import APIRouter, Header, HTTPException
from groq import Groq
from pydantic import BaseModel

router = APIRouter(tags=["summarize"])

SYSTEM_PROMPT = (
    "다음 글을 3~4개의 핵심 불릿으로 요약해줘. "
    "각 줄은 하이픈(-)으로 시작하고, 한 줄에 하나씩만 작성해. "
    "불릿 외의 다른 설명은 절대 붙이지 마. "
    # 언어 지시가 없으면 본문에 영어 용어가 많은 글(기술 글 등)은 가끔 영어로 요약해 버린다.
    "글에 영어가 섞여 있어도 요약은 반드시 한국어로 작성하고, 영어 고유명사·기술 용어만 원문 그대로 둬."
)

MODEL = "openai/gpt-oss-20b"


def strip_html(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html).strip()


class SummarizeRequest(BaseModel):
    title: str
    content: str


@router.post("/summarize")
async def summarize(data: SummarizeRequest, x_internal_secret: str = Header(default="")):
    if x_internal_secret != os.environ.get("INTERNAL_SECRET"):
        raise HTTPException(status_code=401, detail="unauthorized")

    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    plain_content = strip_html(data.content)

    completion = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"제목: {data.title}\n\n본문: {plain_content}"},
        ],
        # openai/gpt-oss-20b는 답변 전에 쓰는 내부 추론 토큰도 max_tokens에 포함돼서,
        # 긴 글이면 512로는 추론만 하다 끝나 빈 답이 올 수 있음(book-info에서 겪은 것과 같은 문제).
        max_tokens=1536,
    )
    text = completion.choices[0].message.content or ""

    bullets = [
        line.lstrip("-•").strip()
        for line in text.splitlines()
        if line.strip().lstrip("-•").strip()
    ]

    if not bullets:
        # 빈 요약을 200으로 돌려주면 호출한 쪽이 기존 요약을 빈 값으로 덮어쓸 수 있음
        raise HTTPException(status_code=502, detail="empty summary from model")

    return {"summary": bullets}
