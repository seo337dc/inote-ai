"""글쓰기 채팅의 초안 도구: 설명은 그대로 답하고, 작성 요청이면 초안 이벤트를 보낸다 (가짜 LLM, 네트워크 없음)."""

import json
import unittest
from typing import List
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import chat


class FakeLLM:
    def __init__(self):
        self.chat_calls: List[tuple] = []   # (messages, tools)
        self.text_calls: List[list] = []
        self.chat_script = [[("text", "도커 볼륨은 데이터를 보존합니다.")]]  # 호출 순서대로 쓸 이벤트 목록
        self.chat_error_first = None
        self.body = ["## 소개\n", "볼륨은 ", "데이터를 보존합니다."]
        self.body_error = False

    def stream_chat(self, messages, tools=None):
        self.chat_calls.append((messages, tools))
        n = len(self.chat_calls) - 1
        if n == 0 and self.chat_error_first:
            raise self.chat_error_first
        script = self.chat_script[min(n, len(self.chat_script) - 1)]
        yield from script

    def stream_text(self, messages, max_tokens=chat.MAX_DRAFT_TOKENS):
        self.text_calls.append(messages)
        if self.body_error:
            raise RuntimeError("llm down")
        yield from self.body


def tool(args, name=chat.DRAFT_TOOL_NAME):
    return ("tool", {"name": name, "arguments": args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)})


def parse(resp):
    out = []
    for block in resp.text.split("\n\n"):
        block = block.strip()
        if not block.startswith("data: "):
            continue
        payload = block[len("data: "):]
        out.append(payload if payload == "[DONE]" else json.loads(payload))
    return out


def kinds(events):
    return [next(iter(e)) if isinstance(e, dict) else e for e in events]


class ChatDraftTest(unittest.TestCase):
    def setUp(self):
        self.llm = FakeLLM()
        app = FastAPI()
        app.include_router(chat.router)
        app.dependency_overrides[chat.get_llm] = lambda: self.llm
        self.client = TestClient(app)
        self.saved: List[tuple] = []
        for name in ("upsert_session", "save_message"):
            p = mock.patch.object(chat, name, side_effect=lambda *a, _n=name, **k: self.saved.append((_n, a, k)))
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(chat, "is_owner", new=mock.AsyncMock(return_value=True))
        p.start()
        self.addCleanup(p.stop)

    def ask(self, text="도커 볼륨 글 써줘", **extra):
        body = {"user_id": "u1", "messages": [{"role": "user", "content": text}], **extra}
        return self.client.post("/chat/stream", json=body)

    # ── 도구를 주는 조건 ──────────────────────────────────────────────────────
    def test_tools_are_only_given_when_can_draft(self):
        self.ask()
        self.assertIsNone(self.llm.chat_calls[0][1])
        self.assertNotIn("start_draft", self.llm.chat_calls[0][0][0]["content"])
        self.ask(can_draft=True)
        messages, tools = self.llm.chat_calls[1]
        self.assertEqual(tools, [chat.DRAFT_TOOL])
        self.assertIn(chat.DRAFT_RULES, messages[0]["content"])

    def test_old_requests_without_the_flag_behave_as_before(self):
        events = parse(self.ask("안녕"))
        self.assertEqual(kinds(events), ["text", "[DONE]"])

    def test_system_prompt_tells_the_model_not_to_make_things_up(self):
        self.assertIn("정확히 알지 못해요", chat.SYSTEM_PROMPT)
        self.assertIn("되물으세요", chat.SYSTEM_PROMPT)

    def test_draft_rules_ask_for_korean_titles_and_a_short_tool_call(self):
        self.assertIn("한국어", chat.DRAFT_RULES)
        self.assertIn("본문은 쓰지 마세요", chat.DRAFT_RULES)

    # ── 설명 질문: 도구를 안 부르면 지금처럼 답한다 ─────────────────────────────
    def test_plain_answer_has_no_draft_events_even_when_drafting_is_allowed(self):
        events = parse(self.ask("도커 볼륨이 뭐야?", can_draft=True))
        self.assertEqual(kinds(events), ["text", "[DONE]"])
        self.assertEqual(self.llm.text_calls, [])  # 본문 생성 호출 없음

    # ── 작성 요청: 도구 호출 → 본문 스트리밍 ───────────────────────────────────
    def test_tool_call_streams_a_draft(self):
        self.llm.chat_script = [[tool({"title": "도커 볼륨 정리", "topic": "도커 볼륨의 개념과 사용법"})]]
        events = parse(self.ask(can_draft=True))
        self.assertEqual(
            kinds(events),
            ["draft_start", "draft_text", "draft_text", "draft_text", "draft_done", "[DONE]"],
        )
        self.assertEqual(events[0], {"draft_start": {"title": "도커 볼륨 정리"}})
        self.assertEqual("".join(e["draft_text"] for e in events if isinstance(e, dict) and "draft_text" in e), "".join(self.llm.body))

    def test_draft_is_written_from_title_topic_and_recent_conversation(self):
        self.llm.chat_script = [[tool({"title": "도커 볼륨 정리", "topic": "볼륨 개념"})]]
        history = [{"role": "user", "content": f"질문{i}"} if i % 2 == 0 else {"role": "assistant", "content": f"답{i}"} for i in range(14)]
        history.append({"role": "user", "content": "위 내용을 글로 써줘"})
        self.client.post("/chat/stream", json={"user_id": "u1", "can_draft": True, "messages": history})
        sent = self.llm.text_calls[0]
        self.assertEqual(sent[0], {"role": "system", "content": chat.WRITER_PROMPT})
        self.assertEqual(len(sent), 1 + chat.MAX_DRAFT_CONTEXT + 1)  # 시스템 + 최근 대화 + 지시
        self.assertIn("제목: 도커 볼륨 정리", sent[-1]["content"])
        self.assertIn("주제: 볼륨 개념", sent[-1]["content"])
        self.assertEqual(sent[-2]["content"], "위 내용을 글로 써줘")
        self.assertNotIn("질문0", json.dumps(sent, ensure_ascii=False))  # 오래된 대화는 뺀다

    def test_text_before_the_tool_call_is_streamed_too(self):
        self.llm.chat_script = [[("text", "초안을 만들어 볼게요."), tool({"title": "t", "topic": "p"})]]
        events = parse(self.ask(can_draft=True))
        self.assertEqual(kinds(events)[:2], ["text", "draft_start"])

    # ── 깨진 인자·실패에도 사용자가 빈손이 되지 않는다 ───────────────────────────
    def test_broken_arguments_fall_back_to_the_users_request(self):
        self.llm.chat_script = [[tool("{not json")]]
        events = parse(self.ask("도커 볼륨 글 써줘", can_draft=True))
        self.assertEqual(events[0], {"draft_start": {"title": "도커 볼륨 글 써줘"}})
        self.assertIn("draft_done", kinds(events))

    def test_missing_title_uses_topic_and_missing_everything_uses_default(self):
        self.assertEqual(chat.parse_draft_args('{"topic": "RAG 개념"}', "x"), ("RAG 개념", "RAG 개념"))
        self.assertEqual(chat.parse_draft_args("", ""), (chat.DEFAULT_DRAFT_TITLE, ""))
        self.assertEqual(chat.parse_draft_args("{}", "  주제  "), ("주제", "주제"))

    def test_unknown_tool_name_is_ignored(self):
        self.llm.chat_script = [[tool({"title": "t", "topic": "p"}, name="delete_everything")]]
        events = parse(self.ask(can_draft=True))
        self.assertEqual(kinds(events), ["[DONE]"])
        self.assertEqual(self.llm.text_calls, [])

    def test_draft_failure_is_reported_and_the_stream_still_ends(self):
        self.llm.chat_script = [[tool({"title": "t", "topic": "p"})]]
        self.llm.body_error = True
        with self.assertLogs(chat.logger, "ERROR"):
            events = parse(self.ask(can_draft=True))
        self.assertEqual(kinds(events), ["draft_start", "draft_failed", "[DONE]"])

    def test_empty_draft_counts_as_failure(self):
        self.llm.chat_script = [[tool({"title": "t", "topic": "p"})]]
        self.llm.body = ["  ", "\n"]
        events = parse(self.ask(can_draft=True))
        self.assertIn("draft_failed", kinds(events))
        self.assertNotIn("draft_done", kinds(events))

    def test_unparseable_tool_call_retries_once_without_tools(self):
        self.llm.chat_error_first = chat.ToolCallFailed("bad")
        self.llm.chat_script = [[], [("text", "도구 없이 답해요.")]]
        with self.assertLogs(chat.logger, "WARNING"):
            events = parse(self.ask(can_draft=True))
        self.assertEqual(kinds(events), ["text", "[DONE]"])
        self.assertEqual(len(self.llm.chat_calls), 2)
        self.assertIsNone(self.llm.chat_calls[1][1])  # 두 번째는 도구 없이

    # ── 저장 ─────────────────────────────────────────────────────────────────
    def test_saved_assistant_message_contains_the_draft(self):
        self.llm.chat_script = [[tool({"title": "도커 볼륨 정리", "topic": "p"})]]
        self.ask(can_draft=True, session_id="s1")
        name, args, _ = self.saved[-1]
        self.assertEqual((name, args[0], args[1]), ("save_message", "s1", "assistant"))
        self.assertTrue(args[2].startswith("**초안: 도커 볼륨 정리**"))
        self.assertIn("데이터를 보존합니다.", args[2])

    def test_saved_message_keeps_text_and_draft_together(self):
        self.llm.chat_script = [[("text", "만들게요."), tool({"title": "t", "topic": "p"})]]
        self.ask(can_draft=True, session_id="s1")
        self.assertTrue(self.saved[-1][1][2].startswith("만들게요.\n\n**초안: t**"))

    def test_failed_draft_is_saved_as_a_short_note(self):
        self.llm.chat_script = [[tool({"title": "t", "topic": "p"})]]
        self.llm.body_error = True
        with self.assertLogs(chat.logger, "ERROR"):
            self.ask(can_draft=True, session_id="s1")
        self.assertEqual(self.saved[-1][1][2], "(초안을 만들지 못했어요)")

    def test_guests_are_not_saved(self):
        self.ask(can_draft=True)  # session_id 없음
        self.assertEqual(self.saved, [])

    def test_post_session_is_saved_only_for_the_owner(self):
        with mock.patch.object(chat, "is_owner", new=mock.AsyncMock(return_value=False)):
            self.ask(session_id="s1", post_id="p1")
        self.assertEqual(self.saved, [])


if __name__ == "__main__":
    unittest.main()
