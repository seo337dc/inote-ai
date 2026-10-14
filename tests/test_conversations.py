"""대화 저장·조회의 SQL 파라미터를 가짜 DB 연결로 확인한다 (실제 DB 없이)."""

import json
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import conversations, sessions


class FakeCursor:
    def __init__(self, rows=None, one=None):
        self.executed = []
        self._rows = rows or []
        self._one = one

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._one

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_connection(cursor):
    class Conn:
        def cursor(self):
            return cursor

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    @contextmanager
    def get_connection():
        yield Conn()

    return get_connection


class UpsertSessionTest(unittest.TestCase):
    def run_upsert(self, **kwargs):
        cur = FakeCursor()
        with mock.patch.object(conversations, "get_connection", fake_connection(cur)):
            conversations.upsert_session("s1", "u1", **kwargs)
        return cur.executed[0]

    def test_kind_defaults_to_write(self):
        _, params = self.run_upsert()
        self.assertEqual(params, ("s1", "u1", None, None, None, "write"))

    def test_kind_is_stored(self):
        _, params = self.run_upsert(kind="posts")
        self.assertEqual(params[-1], "posts")

    def test_unknown_kind_falls_back_to_default(self):
        _, params = self.run_upsert(kind="whatever")
        self.assertEqual(params[-1], "write")

    def test_kind_is_not_overwritten_for_existing_sessions(self):
        sql, _ = self.run_upsert(kind="posts")
        update_part = sql.split("do update set")[1]
        self.assertNotIn("kind", update_part)


class SaveMessageTest(unittest.TestCase):
    def save(self, *args):
        cur = FakeCursor()
        with mock.patch.object(conversations, "get_connection", fake_connection(cur)):
            conversations.save_message("s1", *args)
        return cur.executed

    def test_sources_are_stored_as_json(self):
        sources = [{"id": "a", "title": "도커", "category": "DevOps", "score": 0.6}]
        (_, params), = self.save("assistant", "답", sources)
        self.assertEqual(params[:3], ("s1", "assistant", "답"))
        self.assertEqual(json.loads(params[3]), sources)
        self.assertIn("도커", params[3])  # 한글이 \\u 이스케이프되지 않는다

    def test_no_sources_stores_null(self):
        for sources in (None, []):
            (_, params), = self.save("assistant", "답", sources)
            self.assertIsNone(params[3])

    def test_empty_content_is_not_saved(self):
        self.assertEqual(self.save("assistant", ""), [])

    def test_db_failure_is_swallowed(self):
        @contextmanager
        def broken():
            raise RuntimeError("db down")
            yield

        with mock.patch.object(conversations, "get_connection", broken), self.assertLogs(conversations.logger, "ERROR"):
            conversations.save_message("s1", "user", "질문")  # 던지지 않는다(오류만 기록)


class HistoryAndSessionsApiTest(unittest.TestCase):
    def client(self):
        app = FastAPI()
        app.include_router(conversations.router)
        app.include_router(sessions.router)
        return TestClient(app)

    def test_history_returns_sources_only_when_present(self):
        sources = [{"id": "a", "title": "도커", "category": "DevOps", "score": 0.6}]
        cur = FakeCursor(rows=[("user", "질문", None), ("assistant", "답", sources)], one=("u1",))
        with mock.patch.object(conversations, "get_connection", fake_connection(cur)):
            res = self.client().get("/conversations/s1", params={"user_id": "u1"})
        self.assertEqual(
            res.json(),
            [{"role": "user", "content": "질문"}, {"role": "assistant", "content": "답", "sources": sources}],
        )

    def test_history_of_another_users_session_is_forbidden(self):
        cur = FakeCursor(one=("someone-else",))
        with mock.patch.object(conversations, "get_connection", fake_connection(cur)):
            res = self.client().get("/conversations/s1", params={"user_id": "u1"})
        self.assertEqual(res.status_code, 403)

    def test_session_list_includes_kind(self):
        now = datetime(2026, 10, 14, tzinfo=timezone.utc)
        for q in (None, "도커"):
            cur = FakeCursor(rows=[("s1", None, None, None, now, "마지막 말", "posts")])
            with mock.patch.object(sessions, "get_connection", fake_connection(cur)):
                res = self.client().get("/sessions", params={"user_id": "u1", **({"q": q} if q else {})})
            self.assertEqual(res.json()[0]["kind"], "posts", q)


if __name__ == "__main__":
    unittest.main()
