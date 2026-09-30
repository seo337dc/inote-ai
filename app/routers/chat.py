from __future__ import annotations

import json
import logging
import os
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from groq import Groq
from pydantic import BaseModel

from app.routers.conversations import save_message, upsert_session
from app.services.ownership import is_owner

logger = logging.getLogger(__name__)

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
    session_id: Optional[str] = None
    post_id: Optional[str] = None
    post_title: Optional[str] = None
    post_category: Optional[str] = None
    messages: list[ChatMessage]


@router.post("/stream")
async def chat_stream(data: ChatRequest):
    client = Groq(api_key=os.environ["GROQ_API_KEY"])

    # session_id가 없으면 로그인 안 한 게스트 — 저장 자체를 안 함(휘발성 채팅).
    # session_id가 있으면, post_id가 있는 글쓰기 세션은 요청자가 그 글의 작성자일 때만 저장.
    # post_id가 없는 일반 세션은 항상 저장 (FE가 준 user_id를 그대로 신뢰).
    can_persist = data.session_id is not None
    if can_persist and data.post_id:
        can_persist = await is_owner(data.post_id, data.user_id)

    if can_persist:
        upsert_session(
            data.session_id, data.user_id, data.post_id, data.post_title, data.post_category
        )

    last_user = next((m for m in reversed(data.messages) if m.role == "user"), None)
    if last_user and can_persist:
        save_message(data.session_id, "user", last_user.content)

    groq_messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *[{"role": m.role, "content": m.content} for m in data.messages],
    ]

    def generate():
        try:
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
                save_message(data.session_id, "assistant", full_text)
        except Exception:
            # 스트리밍 도중의 실패는 이미 응답이 시작돼 전역 에러 핸들러로 못 가므로 여기서 남긴다
            logger.exception("chat stream failed (session_id=%s)", data.session_id)
            raise

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
