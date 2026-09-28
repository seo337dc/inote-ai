from __future__ import annotations

import json
import logging
import os
import re

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from groq import Groq
from pydantic import BaseModel

from app.services.book_tools import TOOLS, search_books

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/reading-chat", tags=["reading-chat"])

SYSTEM_PROMPT = (
    "당신은 개인 기록 앱 'iNote'의 독서 상담 AI 어시스턴트입니다. "
    "책 추천, 검색, 가격 문의, 베스트셀러 등 독서 관련 질문에 답해주세요. "
    "특정 책 정보나 가격이 필요하면 search_books 도구로 실제 데이터를 찾아본 뒤 답변하세요. "
    "**구체적인 책 제목·저자를 하나라도 언급(추천 포함)할 때는, 그 책이 실제로 존재하는지 "
    "search_books로 먼저 확인하세요 — 지어낸 제목/저자 조합을 사실처럼 추천하면 절대 안 됩니다.** "
    "확인 안 된 내용은 '정확히는 모르겠지만' 같은 표현으로 명확히 구분해서 말해주세요. "
    "친절하고 간결하게 답변해주세요."
)

MODEL = "openai/gpt-oss-20b"

AVAILABLE_TOOLS = {"search_books": search_books}

# 검색 결과가 실제로 있는데도 모델이 "못 찾았다"고 잘못 답하는 경우가 실측 확인됨
# (gpt-oss-20b가 가끔 도구 결과를 무시함). 조사가 끼어들면(예: "확인할 수는 없습니다")
# 고정 문자열 매칭이 깨져서, "부정 어미"와 "확인/검색 관련 단어"가 각각 등장하는지로 느슨하게 판단.
_NEGATION_MARKERS = ["없", "못"]
_LOOKUP_MARKERS = ["찾", "확인", "검색 결과", "정보"]


def _looks_like_refusal(text: str) -> bool:
    return any(n in text for n in _NEGATION_MARKERS) and any(
        m in text for m in _LOOKUP_MARKERS
    )


class ChatMessage(BaseModel):
    role: str
    content: str


class ReadingChatRequest(BaseModel):
    messages: list[ChatMessage]


@router.post("/stream")
async def reading_chat_stream(data: ReadingChatRequest):
    client = Groq(api_key=os.environ["GROQ_API_KEY"])

    groq_messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *[{"role": m.role, "content": m.content} for m in data.messages],
    ]

    # 1단계: 도구 호출이 필요한지 먼저 판단(non-stream) — 필요하면 실제로 실행해서
    # 결과를 대화에 반영한 뒤, 2단계에서 그 결과를 바탕으로 최종 답변을 스트리밍한다.
    planning = client.chat.completions.create(
        model=MODEL,
        messages=groq_messages,
        tools=TOOLS,
        tool_choice="auto",
        max_tokens=1024,
    )
    message = planning.choices[0].message

    has_nonempty_result = False
    if message.tool_calls:
        # 정석대로 role: "assistant"(tool_calls 포함) + role: "tool" 메시지를 그대로 다시
        # 넘기면, gpt-oss-20b가 2단계(tools 파라미터를 안 줬는데도)에서 또 tool_call을
        # 시도해서 "Tool choice is none, but model called a tool" 에러가 남 —
        # tool_choice="none"으로 명시해봐도 이 모델은 무시하고 또 시도함(재현 확인).
        # 그래서 OpenAI 프로토콜 그대로 넘기지 않고, 검색 결과를 그냥 평범한 텍스트로 요약해서
        # system 메시지 하나로 끼워 넣는다 — 2단계는 tools 자체를 몰라서 다시 부를 수가 없음.
        tool_results = []
        for tc in message.tool_calls:
            fn = AVAILABLE_TOOLS.get(tc.function.name)
            args = json.loads(tc.function.arguments or "{}")
            result = await fn(**args) if fn else {"error": "unknown tool"}
            if isinstance(result, list) and result:
                has_nonempty_result = True
            tool_results.append({"tool": tc.function.name, "args": args, "result": result})

        groq_messages.append(
            {
                "role": "system",
                "content": (
                    "다음은 방금 도구를 호출해서 얻은 실제 검색 결과다. 이 데이터는 100% 정확하니 "
                    "반드시 이 안의 정보를 사용해서 답변해. result가 빈 배열이 아니면 절대 "
                    "'찾지 못했다'거나 '정보가 없다'고 말하지 말고, 아래 항목들을 근거로 구체적으로 "
                    "답변해. 도구를 다시 호출하지는 마:\n"
                    + json.dumps(tool_results, ensure_ascii=False)
                ),
            }
        )

    headers = {"Cache-Control": "no-cache", "Connection": "keep-alive"}

    if not has_nonempty_result:
        # 도구를 안 썼거나(일반 대화), 도구 결과가 원래 비어있는 경우 — 그대로 실시간 스트리밍.
        def generate():
            stream = client.chat.completions.create(
                model=MODEL,
                messages=groq_messages,
                max_tokens=1024,
                stream=True,
            )
            for chunk in stream:
                text = chunk.choices[0].delta.content or ""
                if text:
                    yield f"data: {json.dumps({'text': text})}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(generate(), media_type="text/event-stream", headers=headers)

    # 도구 결과가 실제로 있는 경우엔, 모델이 그 결과를 무시하고 "못 찾았다"고 답하는 경우가
    # 실측으로 확인됨 — 먼저 non-stream으로 받아서 검증하고, 그런 문구가 보이면 한 번 더
    # 강하게 재시도한 뒤 내려준다. 완성된 텍스트를 청크로 쪼개 SSE 포맷으로 흉내내서
    # 보내므로, 클라이언트 입장에서는 실시간 스트리밍과 구분되지 않는다.
    final = client.chat.completions.create(model=MODEL, messages=groq_messages, max_tokens=1024)
    final_text = final.choices[0].message.content or ""
    if _looks_like_refusal(final_text):
        logger.warning("model ignored non-empty tool result, retrying once")
        groq_messages.append(
            {
                "role": "system",
                "content": "방금 답변에서 도구 결과를 무시했다. 위 검색 결과 데이터를 반드시 근거로 삼아 다시 답변해.",
            }
        )
        retry = client.chat.completions.create(model=MODEL, messages=groq_messages, max_tokens=1024)
        final_text = retry.choices[0].message.content or final_text

    def generate_from_text():
        for piece in re.split(r"(\s+)", final_text):
            if piece:
                yield f"data: {json.dumps({'text': piece})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate_from_text(), media_type="text/event-stream", headers=headers)
