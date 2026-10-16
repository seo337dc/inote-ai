from __future__ import annotations

import json
import logging
import os
from typing import Iterator, List, Optional, Tuple

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from groq import BadRequestError, Groq
from pydantic import BaseModel

from app.routers.conversations import save_message, upsert_session
from app.services.ownership import is_owner

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])

SYSTEM_PROMPT = (
    "당신은 개인 기록 앱 'iNote'의 AI 어시스턴트입니다. "
    "사용자가 할일·일지·독서·글쓰기를 기록하는 것을 돕습니다. "
    "친절하고 간결하게 답변해주세요. "
    "정확히 알지 못하는 제품명·서비스명·고유명사·용어는 그럴듯하게 설명하지 말고 '정확히 알지 못해요'라고 먼저 말한 뒤, "
    "어떤 맥락(분야, 어디서 들었는지)인지 되물으세요. 아는 일반 개념은 평소처럼 설명하세요."
)

# 글쓰기 화면(can_draft=true)에서만 붙는 규칙 — 설명과 글 작성을 모델이 도구 호출로 스스로 구분한다.
# 24개 예문(설명·함정·애매·작성)으로 판단 정확도를 확인했다: scripts/eval_draft_intent.py
DRAFT_RULES = (
    "\n\n[글 초안 도구 사용 규칙]\n"
    "- start_draft 도구는 사용자가 글(게시물) 작성을 분명히 요청했고"
    "(예: '글 써줘', '초안 작성해줘', '글로 정리해줘') 글의 주제를 알 수 있을 때만 호출하세요.\n"
    "- 개념 설명, 질문, 조언, 글감 추천, 글쓰기 방법 문의는 도구 없이 일반 답변을 하세요.\n"
    "- 글을 쓰려는 것 같지만 주제가 없거나 애매하면 도구를 호출하지 말고 '어떤 주제로 쓸까요?'처럼 먼저 되물으세요.\n"
    "- 글 제목은 사용자가 다른 언어를 요구하지 않는 한 한국어로 쓰세요.\n"
    "- 도구에는 제목과 주제 요약만 넣고 본문은 쓰지 마세요. 본문은 도구 호출 뒤에 따로 작성합니다."
)

DRAFT_TOOL_NAME = "start_draft"
# 본문은 도구 인자에 넣지 않는다: 긴 마크다운을 JSON 인자에 담게 하면 약 9%가 파싱 실패(400)로 끝났다.
DRAFT_TOOL = {
    "type": "function",
    "function": {
        "name": DRAFT_TOOL_NAME,
        "description": (
            "사용자가 글(게시물) 작성을 분명히 요청했고 주제를 알 수 있을 때만 호출한다. "
            "글 본문은 이 도구 호출 뒤에 따로 작성하므로 여기서는 쓰지 않는다."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "글 제목"},
                "topic": {"type": "string", "description": "글의 주제와 다룰 내용을 한두 문장으로"},
            },
            "required": ["title", "topic"],
        },
    },
}

WRITER_PROMPT = (
    "당신은 한국어 블로그 글 작성자입니다. 주어진 제목과 주제로 글 본문을 마크다운으로 작성하세요. "
    "제목은 본문에 다시 쓰지 말고, 소제목(##)과 목록, 필요하면 코드 블록을 사용하세요. "
    "확실하지 않은 사실은 단정하지 말고, 대화에 없는 개인 경험이나 수치를 지어내지 마세요. "
    "본문만 출력하고 인사말이나 설명은 쓰지 마세요."
)

MODEL = "openai/gpt-oss-20b"
MAX_DRAFT_CONTEXT = 10  # 초안을 쓸 때 참고하는 최근 대화 수
MAX_DRAFT_TOKENS = 4096
DEFAULT_DRAFT_TITLE = "새 글"


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    user_id: str
    session_id: Optional[str] = None
    post_id: Optional[str] = None
    post_title: Optional[str] = None
    post_category: Optional[str] = None
    # true면 모델이 글 초안 도구를 쓸 수 있다(글쓰기 화면). 기본 false라 기존 요청은 달라지지 않는다.
    can_draft: bool = False
    messages: list[ChatMessage]


class ToolCallFailed(Exception):
    """모델이 만든 도구 호출을 서버가 해석하지 못함 — 도구 없이 다시 답하게 한다."""


class GroqLLM:
    def _client(self) -> Groq:
        return Groq(api_key=os.environ["GROQ_API_KEY"])

    def stream_chat(self, messages: List[dict], tools: Optional[List[dict]] = None) -> Iterator[Tuple[str, object]]:
        """("text", 조각)을 흘리고, 모델이 도구를 불렀으면 마지막에 ("tool", {"name", "arguments"}) 하나."""
        kwargs = dict(model=MODEL, messages=messages, max_tokens=1024, stream=True)
        if tools:
            kwargs.update(tools=tools, tool_choice="auto")
        calls: dict = {}
        try:
            for chunk in self._client().chat.completions.create(**kwargs):
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                text = delta.content or ""
                if text:
                    yield ("text", text)
                for call in getattr(delta, "tool_calls", None) or []:
                    slot = calls.setdefault(call.index, {"name": "", "arguments": ""})
                    if call.function:
                        slot["name"] += call.function.name or ""
                        slot["arguments"] += call.function.arguments or ""
        except BadRequestError as e:
            if tools and "tool" in str(e).lower():
                raise ToolCallFailed(str(e)[:200]) from e
            raise
        if calls:
            yield ("tool", calls[min(calls)])

    def stream_text(self, messages: List[dict], max_tokens: int = MAX_DRAFT_TOKENS) -> Iterator[str]:
        for chunk in self._client().chat.completions.create(
            model=MODEL, messages=messages, max_tokens=max_tokens, stream=True
        ):
            if not chunk.choices:
                continue
            text = chunk.choices[0].delta.content or ""
            if text:
                yield text


def get_llm() -> GroqLLM:
    """FastAPI 의존성 — 테스트에서 dependency_overrides로 가짜를 넣는다."""
    return GroqLLM()


def _sse(payload) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def parse_draft_args(raw: str, fallback_topic: str) -> Tuple[str, str]:
    """도구 인자에서 (제목, 주제). 깨져 있거나 비어 있어도 쓸 수 있는 값을 돌려준다."""
    try:
        args = json.loads(raw) if raw else {}
    except ValueError:
        args = {}
    title = str(args.get("title") or "").strip()
    topic = str(args.get("topic") or "").strip() or fallback_topic.strip()
    return (title or topic[:40] or DEFAULT_DRAFT_TITLE), topic


def writer_messages(history: List[dict], title: str, topic: str) -> List[dict]:
    """초안 본문을 쓰는 호출의 메시지 — 최근 대화를 참고하게 하고, 마지막에 제목·주제를 못 박는다."""
    recent = [m for m in history if m["role"] in ("user", "assistant")][-MAX_DRAFT_CONTEXT:]
    return [
        {"role": "system", "content": WRITER_PROMPT},
        *recent,
        {"role": "user", "content": f"위 대화를 참고해서 아래 글의 본문을 작성하세요.\n제목: {title}\n주제: {topic}"},
    ]


def _turn(llm, messages: List[dict], tools: Optional[List[dict]]) -> Iterator[Tuple[str, object]]:
    """한 번의 답변 — 도구 호출을 해석하지 못하면(아직 아무것도 보내기 전이라면) 도구 없이 다시 답한다."""
    emitted = False
    try:
        for event in llm.stream_chat(messages, tools):
            emitted = True
            yield event
    except ToolCallFailed:
        if emitted:
            raise
        logger.warning("tool call could not be parsed; answering without tools")
        yield from llm.stream_chat(messages, None)


@router.post("/stream")
async def chat_stream(data: ChatRequest, llm: GroqLLM = Depends(get_llm)):
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

    history = [{"role": m.role, "content": m.content} for m in data.messages]
    system = SYSTEM_PROMPT + (DRAFT_RULES if data.can_draft else "")
    tools = [DRAFT_TOOL] if data.can_draft else None
    groq_messages = [{"role": "system", "content": system}, *history]

    def generate():
        try:
            full_text = ""
            tool_call = None
            for kind, value in _turn(llm, groq_messages, tools):
                if kind == "text":
                    full_text += value
                    yield _sse({"text": value})
                elif kind == "tool":
                    tool_call = value

            saved_draft = ""
            if tool_call and tool_call.get("name") == DRAFT_TOOL_NAME and data.can_draft:
                title, topic = parse_draft_args(tool_call.get("arguments", ""), last_user.content if last_user else "")
                yield _sse({"draft_start": {"title": title}})
                body = ""
                try:
                    for piece in llm.stream_text(writer_messages(history, title, topic)):
                        body += piece
                        yield _sse({"draft_text": piece})
                except Exception:
                    logger.exception("draft generation failed (session_id=%s)", data.session_id)
                    body = ""
                if body.strip():
                    yield _sse({"draft_done": True})
                    saved_draft = f"**초안: {title}**\n\n{body}"
                else:
                    yield _sse({"draft_failed": True})
                    saved_draft = "(초안을 만들지 못했어요)"

            yield "data: [DONE]\n\n"
            if can_persist:
                parts = [p for p in (full_text.strip(), saved_draft) if p]
                save_message(data.session_id, "assistant", "\n\n".join(parts))
        except Exception:
            # 스트리밍 도중의 실패는 이미 응답이 시작돼 전역 에러 핸들러로 못 가므로 여기서 남긴다
            logger.exception("chat stream failed (session_id=%s)", data.session_id)
            raise

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
