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
    "불릿 외의 다른 설명은 절대 붙이지 마."
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
        max_tokens=512,
    )
    text = completion.choices[0].message.content or ""

    bullets = [
        line.lstrip("-•").strip()
        for line in text.splitlines()
        if line.strip().lstrip("-•").strip()
    ]

    return {"summary": bullets}
