from __future__ import annotations

import json
import os
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from groq import Groq
from pydantic import BaseModel

from app.routers.conversations import save_message
from app.services.ownership import is_owner

router = APIRouter(prefix="/chat", tags=["chat"])

SYSTEM_PROMPT = (
    "당신은 개인 기록 앱 'iNote'의 AI 어시스턴트입니다. "
    "사용자가 할일·일지·독서·글쓰기를 기록하는 것을 돕습니다. "
    "친절하고 간결하게 답변해주세요."
)

MODEL = "openai/gpt-oss-20b"


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    user_id: str
    post_id: Optional[str] = None
    messages: list[ChatMessage]


@router.post("/stream")
async def chat_stream(data: ChatRequest):
    client = Groq(api_key=os.environ["GROQ_API_KEY"])

    # post_id가 있어도 요청자가 그 글의 작성자가 아니면 채팅 자체는 그대로 흘려보내고
    # 저장(대화 기록 연결)만 하지 않는다 — 다른 사람 글의 대화 기록을 훔쳐 쓰지 못하게.
    can_persist = bool(data.post_id) and await is_owner(data.post_id, data.user_id)

    last_user = next((m for m in reversed(data.messages) if m.role == "user"), None)
    if last_user and can_persist:
        save_message(data.post_id, data.user_id, "user", last_user.content)

    groq_messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *[{"role": m.role, "content": m.content} for m in data.messages],
    ]

    def generate():
        full_text = ""
        stream = client.chat.completions.create(
            model=MODEL,
            messages=groq_messages,
            max_tokens=1024,
            stream=True,
        )
        for chunk in stream:
            text = chunk.choices[0].delta.content or ""
            if text:
                full_text += text
                yield f"data: {json.dumps({'text': text})}\n\n"
        yield "data: [DONE]\n\n"
        if can_persist:
            save_message(data.post_id, data.user_id, "assistant", full_text)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
