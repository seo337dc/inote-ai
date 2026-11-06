"""임베딩 API 테스트 — 가짜 저장소·가짜 임베딩 서비스로 돈다 (DB·네트워크 없음).
후보 id 제한이 SQL에서 실제로 지켜지는지는 tests/test_embedding_store_db.py(실제 pgvector)가 확인한다."""

import os
import unittest
from datetime import datetime, timezone
from typing import List, Optional
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import embeddings
from app.services.embedder_client import EXPECTED_DIM, EmbedderClient, EmbedderHealth, EmbedderUnavailable, EmbedResult, expected_model, get_embedder
from app.services.embedding_jobs import JobStatus, get_job_store
from app.services.embedding_store import Match, get_store
from app.services.text_prep import build_embedding_text, content_hash, html_to_text

SECRET = "test-secret"
HEADERS = {"x-internal-secret": SECRET}
MODEL = "nlpai-lab/KURE-v1"


class FakeEmbedder:
    def __init__(self):
        self.calls: List[List[str]] = []
        self.down = False

    def health(self):
        return EmbedderHealth(reachable=not self.down, model=None if self.down else MODEL)

    def embed(self, texts):
        self.calls.append(list(texts))
        if self.down:
            raise EmbedderUnavailable("임베딩 서비스를 쓸 수 없습니다")
        return EmbedResult(model=MODEL, dim=EXPECTED_DIM, embeddings=[[float(len(t))] + [0.0] * (EXPECTED_DIM - 1) for t in texts])


class FakeStore:
    """저장소 호출 기록용. similar_*의 결과는 테스트가 정해 준다."""

    def __init__(self):
        self.rows = {}
        self.chunks = {}  # post_id -> (model, source_hash, [ChunkRow, ...])
        self.upserts = []
        self.writes = []
        self.similar_calls = []
        self.chunk_info = {}  # post_id -> (조각 수, 임베딩 시각) — status 응답용
        self.post_matches: Optional[List[Match]] = []
        self.vector_matches: List[Match] = []

    def get_state(self, post_id):
        row = self.rows.get(post_id)
        return (row[0], row[1]) if row else None

    def get_chunk_state(self, post_id):
        row = self.chunks.get(post_id)
        return (row[0], row[1], len(row[2])) if row else None

    def upsert(self, post_id, model, digest, vector):
        self.rows[post_id] = (model, digest, list(vector))
        self.upserts.append(post_id)

    def write(self, post_id, model, post, chunks, source_hash):
        self.writes.append(post_id)
        if post is not None:
            self.upsert(post_id, model, post[0], post[1])
        if chunks is not None:
            if chunks:
                self.chunks[post_id] = (model, source_hash, list(chunks))
            else:
                self.chunks.pop(post_id, None)

    def delete(self, post_id):
        deleted_post = self.rows.pop(post_id, None) is not None
        deleted_chunks = self.chunks.pop(post_id, None) is not None
        return deleted_post or deleted_chunks

    def chunk_summary(self, post_ids):
        return {p: self.chunk_info[p] for p in post_ids if p in self.chunk_info}

    def similar_to_post(self, post_id, candidate_ids, limit, min_score):
        self.similar_calls.append(("post", post_id, list(candidate_ids), limit, min_score))
        return self.post_matches

    def similar_to_vector(self, vector, candidate_ids, limit, min_score):
        self.similar_calls.append(("vector", list(vector)[:1], list(candidate_ids), limit, min_score))
        return self.vector_matches


class FakeJobStore:
    """작업 대기열 호출 기록용. fail_*로 DB 오류를 흉내 낸다."""

    def __init__(self):
        self.jobs = {}  # post_id -> {"title", "content", "version"}
        self.fail_enqueue = False
        self.fail_delete = False
        self.job_status = {}  # post_id -> JobStatus — status 응답용

    def statuses(self, post_ids):
        return {p: self.job_status[p] for p in post_ids if p in self.job_status}

    def enqueue(self, post_id, title, content):
        if self.fail_enqueue:
            raise RuntimeError("db down")
        version = self.jobs.get(post_id, {}).get("version", 0) + 1
        self.jobs[post_id] = {"title": title, "content": content, "version": version}
        return version

    def delete(self, post_id):
        if self.fail_delete:
            raise RuntimeError("db down")
        return self.jobs.pop(post_id, None) is not None


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"INTERNAL_SECRET": SECRET})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store, self.embedder, self.jobs = FakeStore(), FakeEmbedder(), FakeJobStore()
        app = FastAPI()
        app.include_router(embeddings.router)
        app.dependency_overrides[get_store] = lambda: self.store
        app.dependency_overrides[get_embedder] = lambda: self.embedder
        app.dependency_overrides[get_job_store] = lambda: self.jobs
        self.client = TestClient(app)


class AuthTest(ApiTestCase):
    ENDPOINTS = (
        ("put", "/embeddings/posts/p1", {"title": "t", "content": "<p>c</p>"}),
        ("delete", "/embeddings/posts/p1", None),
        ("post", "/embeddings/similar", {"postId": "p1", "candidateIds": ["a"]}),
        ("post", "/embeddings/status", {"postIds": ["p1"]}),
        ("get", "/embeddings/health", None),
    )

    def call(self, method, path, body, headers):
        return self.client.request(method, path, json=body, headers=headers)

    def test_비밀키가_없거나_틀리면_401이고_아무것도_실행하지_않는다(self):
        for method, path, body in self.ENDPOINTS:
            for headers in ({}, {"x-internal-secret": "wrong"}, {"x-internal-secret": ""}):
                with self.subTest(path=path, method=method, headers=headers):
                    self.assertEqual(self.call(method, path, body, headers).status_code, 401)
        self.assertEqual((self.embedder.calls, self.store.upserts, self.store.similar_calls), ([], [], []))

    def test_서버에_비밀키가_설정돼_있지_않으면_모든_요청을_거절한다(self):
        with mock.patch.dict(os.environ, {"INTERNAL_SECRET": ""}):
            for method, path, body in self.ENDPOINTS:
                with self.subTest(path=path, method=method):
                    self.assertEqual(self.call(method, path, body, HEADERS).status_code, 503)
                    self.assertEqual(self.call(method, path, body, {"x-internal-secret": ""}).status_code, 503)


class UpsertTest(ApiTestCase):
    def put(self, post_id="p1", title="제목", content="<p>본문</p>"):
        return self.client.put(f"/embeddings/posts/{post_id}", json={"title": title, "content": content}, headers=HEADERS)

    def test_새_글은_임베딩해서_저장한다(self):
        res = self.put(title="도커", content="<h1>시작</h1><p>컨테이너</p>")

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"status": "embedded", "model": MODEL, "dim": EXPECTED_DIM, "chunks": 1})
        expected_text = build_embedding_text("도커", html_to_text("<h1>시작</h1><p>컨테이너</p>"))
        # 글 벡터용 텍스트 1개 + 조각 1개(짧은 글은 조각이 하나뿐이고, 제목+조각이 글 텍스트와 같다)를 한 번에 보낸다. HTML 태그는 지운다
        self.assertEqual(self.embedder.calls, [[expected_text, expected_text]])
        model, digest, _ = self.store.rows["p1"]
        self.assertEqual((model, digest), (MODEL, content_hash(expected_text)))

    def test_같은_내용을_다시_저장하면_임베딩_서비스를_부르지_않는다(self):
        self.put()
        res = self.put()

        self.assertEqual(res.json()["status"], "unchanged")
        self.assertEqual(len(self.embedder.calls), 1)
        self.assertEqual(self.store.upserts, ["p1"])

    def test_내용이_바뀌면_다시_임베딩한다(self):
        self.put(content="<p>하나</p>")
        res = self.put(content="<p>둘</p>")

        self.assertEqual(res.json()["status"], "embedded")
        self.assertEqual(len(self.embedder.calls), 2)

    def test_서식만_바뀌고_글자가_같으면_다시_임베딩하지_않는다(self):
        self.put(content="<p>같은 글자</p>")
        res = self.put(content="<div><strong>같은 글자</strong></div>")

        self.assertEqual(res.json()["status"], "unchanged")

    def test_해시가_같아도_모델이_다르면_다시_임베딩한다(self):
        self.put()
        digest = self.store.rows["p1"][1]
        self.store.rows["p1"] = ("old/model", digest, [0.0])

        res = self.put()

        self.assertEqual(res.json()["status"], "embedded")
        self.assertEqual(self.store.rows["p1"][0], MODEL)

    def test_기대_모델은_환경_변수로_바꿀_수_있고_다르면_다시_계산한다(self):
        self.put()
        with mock.patch.dict(os.environ, {"EMBEDDER_MODEL": "other/model"}):
            self.assertEqual(expected_model(), "other/model")
            self.assertEqual(self.put().json()["status"], "embedded")  # 저장된 MODEL과 다르므로

    def test_내용이_비어_있으면_422이고_임베딩하지_않는다(self):
        for title, content in (("", ""), ("   ", "<p> </p>"), ("", "<script>x</script>")):
            with self.subTest(title=title, content=content):
                self.assertEqual(self.put(title=title, content=content).status_code, 422)
        self.assertEqual(self.embedder.calls, [])

    # --- 임베딩 서비스를 쓸 수 없을 때: 대기열 ---
    def test_임베딩_서비스를_쓸_수_없으면_벡터는_저장하지_않고_대기열에_남기고_202(self):
        self.embedder.down = True

        res = self.put(title="제목", content="<p>본문</p>")

        self.assertEqual(res.status_code, 202)
        self.assertEqual(res.json()["status"], "queued")
        self.assertEqual(self.store.rows, {})
        self.assertEqual(self.jobs.jobs, {"p1": {"title": "제목", "content": "<p>본문</p>", "version": 1}})  # 원문 HTML 그대로

    def test_임베딩_서비스_주소가_없어도_글_저장은_성공하고_작업이_남는다(self):
        # 운영(Vercel)은 임베딩 서비스에 닿지 못한다는 전제 — EMBEDDER_URL이 비어 있는 실제 클라이언트로 확인한다
        self.client.app.dependency_overrides[get_embedder] = lambda: EmbedderClient(base_url="", secret="")

        res = self.put()

        self.assertEqual((res.status_code, res.json()["status"]), (202, "queued"))
        self.assertIn("p1", self.jobs.jobs)

    def test_같은_글을_다시_저장하면_대기_작업을_갱신하고_하나만_남는다(self):
        self.embedder.down = True
        self.put(content="<p>하나</p>")

        self.put(content="<p>둘</p>")

        self.assertEqual(list(self.jobs.jobs), ["p1"])
        self.assertEqual((self.jobs.jobs["p1"]["content"], self.jobs.jobs["p1"]["version"]), ("<p>둘</p>", 2))

    def test_대기열에도_남기지_못하면_503이다(self):
        self.embedder.down = True
        self.jobs.fail_enqueue = True

        res = self.put()

        self.assertEqual(res.status_code, 503)
        self.assertEqual(self.store.rows, {})

    def test_임베딩에_성공하면_그_글의_대기_작업을_지운다(self):
        self.jobs.jobs["p1"] = {"title": "옛 제목", "content": "<p>옛 내용</p>", "version": 3}
        self.jobs.jobs["p2"] = {"title": "다른 글", "content": "<p>x</p>", "version": 1}

        res = self.put()

        self.assertEqual(res.json()["status"], "embedded")
        self.assertEqual(list(self.jobs.jobs), ["p2"])  # 다른 글의 작업은 그대로

    def test_벡터를_쓰기_전에_대기_작업을_먼저_지운다(self):
        # 워커가 같은 글을 처리 중이면 그 완료를 기다렸다가 지운 뒤 최신 벡터를 써야, 워커의 오래된 벡터가 최신 벡터를 덮어쓰지 못한다
        log = []
        original_delete, original_upsert = self.jobs.delete, self.store.upsert
        self.jobs.delete = lambda post_id: (log.append("job.delete"), original_delete(post_id))[1]
        self.store.upsert = lambda *args: (log.append("store.upsert"), original_upsert(*args))[1]

        self.put()

        self.assertEqual(log, ["job.delete", "store.upsert"])

    def test_내용이_같아_건너뛸_때도_그_글의_대기_작업을_지운다(self):
        self.put()
        self.jobs.jobs["p1"] = {"title": "t", "content": "<p>c</p>", "version": 2}

        res = self.put()

        self.assertEqual(res.json()["status"], "unchanged")
        self.assertEqual(self.jobs.jobs, {})

    def test_대기_작업_정리가_실패해도_저장_응답은_성공이다(self):
        self.jobs.fail_delete = True

        res = self.put()

        self.assertEqual((res.status_code, res.json()["status"]), (200, "embedded"))
        self.assertIn("p1", self.store.rows)

    def test_내용이_비어_있으면_대기열에도_남기지_않는다(self):
        self.embedder.down = True

        self.assertEqual(self.put(title="", content="").status_code, 422)
        self.assertEqual(self.jobs.jobs, {})

    def test_입력_크기_상한(self):
        self.assertEqual(self.put(title="가" * (embeddings.MAX_TITLE_CHARS + 1)).status_code, 422)
        self.assertEqual(self.put(content="가" * (embeddings.MAX_CONTENT_CHARS + 1)).status_code, 422)

    def test_필수_필드가_빠지면_422(self):
        self.assertEqual(self.client.put("/embeddings/posts/p1", json={"title": "t"}, headers=HEADERS).status_code, 422)
        self.assertEqual(self.client.put("/embeddings/posts/p1", json={}, headers=HEADERS).status_code, 422)


class ChunkUpsertTest(ApiTestCase):
    """글 벡터와 함께 조각(청크) 벡터를 저장하는 동작."""

    def put(self, post_id="p1", title="제목", content="<p>본문</p>"):
        return self.client.put(f"/embeddings/posts/{post_id}", json={"title": title, "content": content}, headers=HEADERS)

    @staticmethod
    def long_html(paragraphs=30, word="가나다라"):
        return "".join(f"<p>문단{n} " + word * 60 + "</p>" for n in range(paragraphs))

    def test_긴_글은_여러_조각으로_나뉘어_글_벡터와_함께_한_번에_저장된다(self):
        res = self.put(content=self.long_html())

        self.assertEqual(res.status_code, 200)
        count = res.json()["chunks"]
        self.assertGreater(count, 3)
        self.assertEqual(self.store.writes, ["p1"])  # 글 벡터와 조각이 한 번의 쓰기(= 한 트랜잭션)로 들어간다
        model, _, rows = self.store.chunks["p1"]
        self.assertEqual((model, len(rows)), (MODEL, count))
        self.assertEqual([r.idx for r in rows], list(range(count)))  # 글 순서대로 0부터

    def test_조각의_위치는_원문_텍스트를_그대로_가리키고_본문은_저장하지_않는다(self):
        html = self.long_html()
        self.put(title="도커", content=html)

        plain = html_to_text(html)
        for r in self.store.chunks["p1"][2]:
            self.assertTrue(plain[r.start : r.end].startswith(("문단", "가나다라")))
            self.assertFalse(hasattr(r, "text"))

    def test_임베딩_입력이_많으면_16개씩_나눠_보낸다(self):
        res = self.put(content=self.long_html(paragraphs=120))

        self.assertGreater(res.json()["chunks"] + 1, 16)
        self.assertTrue(all(len(c) <= 16 for c in self.embedder.calls))
        self.assertEqual(sum(len(c) for c in self.embedder.calls), res.json()["chunks"] + 1)  # 글 벡터 1 + 조각들

    def test_같은_글을_다시_저장하면_조각도_다시_만들지_않는다(self):
        self.put(content=self.long_html())
        res = self.put(content=self.long_html())

        self.assertEqual(res.json()["status"], "unchanged")
        self.assertEqual(self.store.writes, ["p1"])  # 두 번째 저장은 쓰기도 하지 않았다
        self.assertEqual(len(self.embedder.calls), 1)  # 임베딩 서비스도 한 번만 불렀다

    def test_글이_바뀌면_조각이_통째로_바뀐다(self):
        self.put(content=self.long_html(paragraphs=30))
        before = self.store.chunks["p1"][2]
        self.put(content=self.long_html(paragraphs=10, word="마바사"))
        after = self.store.chunks["p1"][2]

        self.assertLess(len(after), len(before))
        self.assertEqual([r.idx for r in after], list(range(len(after))))

    def test_조각_기능_이전에_저장된_글은_조각만_새로_만든다(self):
        html = self.long_html(paragraphs=6)
        self.store.rows["p1"] = (expected_model(), content_hash(build_embedding_text("제목", html_to_text(html))), [0.0])

        res = self.put(content=html)

        self.assertEqual(res.json()["status"], "embedded")
        self.assertEqual(self.store.upserts, [])  # 글 벡터는 이미 최신이라 다시 쓰지 않는다
        self.assertEqual(sum(len(c) for c in self.embedder.calls), res.json()["chunks"])  # 글 벡터용 입력은 없다
        self.assertEqual(len(self.store.chunks["p1"][2]), res.json()["chunks"])

    def test_도중에_임베딩이_실패하면_아무것도_저장하지_않고_대기열에_남긴다(self):
        original = self.embedder.embed
        state = {"n": 0}

        def flaky(texts):
            state["n"] += 1
            if state["n"] == 2:
                raise EmbedderUnavailable("두 번째 묶음에서 실패")
            return original(texts)

        self.embedder.embed = flaky
        res = self.put(content=self.long_html(paragraphs=120))  # 16개씩 여러 번 나눠 보낸다

        self.assertEqual(res.status_code, 202)
        self.assertEqual((self.store.writes, self.store.rows, self.store.chunks), ([], {}, {}))  # 일부만 저장된 상태가 없다
        self.assertIn("p1", self.jobs.jobs)

    def test_본문이_비어_제목만_있는_글은_글_벡터만_있고_조각은_없다(self):
        res = self.put(title="제목만", content="")

        self.assertEqual(res.json()["chunks"], 0)
        self.assertIn("p1", self.store.rows)
        self.assertNotIn("p1", self.store.chunks)

    def test_삭제하면_조각도_함께_지운다(self):
        self.put(content=self.long_html())
        res = self.client.delete("/embeddings/posts/p1", headers=HEADERS)

        self.assertTrue(res.json()["deleted"])
        self.assertNotIn("p1", self.store.chunks)


class StatusTest(ApiTestCase):
    def post(self, ids):
        return self.client.post("/embeddings/status", json={"postIds": ids}, headers=HEADERS)

    def test_임베딩도_작업도_없는_글은_빈_상태로_나온다(self):
        res = self.post(["p1"])
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["items"]["p1"], {"chunkCount": 0, "embeddedAt": None, "job": None})
        self.assertEqual(res.json()["model"], expected_model())

    def test_조각_수와_임베딩_시각과_작업_상태를_글별로_합쳐서_돌려준다(self):
        at = datetime(2026, 11, 4, 8, 0, tzinfo=timezone.utc)
        self.store.chunk_info["p1"] = (3, at)
        self.jobs.job_status["p2"] = JobStatus(status="failed", attempts=3, last_error="timeout")
        items = self.post(["p1", "p2"]).json()["items"]
        self.assertEqual(items["p1"], {"chunkCount": 3, "embeddedAt": at.isoformat(), "job": None})
        self.assertEqual(items["p2"]["job"], {"status": "failed", "attempts": 3, "lastError": "timeout"})

    def test_요청하지_않은_글은_나오지_않고_중복_id는_한_번만_나온다(self):
        self.store.chunk_info["other"] = (1, datetime(2026, 1, 1, tzinfo=timezone.utc))
        res = self.post(["p1", "p1"])
        self.assertEqual(list(res.json()["items"]), ["p1"])

    def test_글_본문은_응답에_들어가지_않는다(self):
        self.jobs.job_status["p1"] = JobStatus(status="pending", attempts=0, last_error=None)
        self.assertNotIn("content", self.post(["p1"]).json()["items"]["p1"]["job"])

    def test_id가_상한을_넘으면_422이고_빈_목록은_빈_결과다(self):
        self.assertEqual(self.post([f"p{i}" for i in range(embeddings.MAX_CANDIDATES + 1)]).status_code, 422)
        self.assertEqual(self.post([]).json()["items"], {})


class HealthTest(ApiTestCase):
    def test_임베딩_서비스에_닿으면_reachable과_모델을_돌려준다(self):
        res = self.client.get("/embeddings/health", headers=HEADERS)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"embedder": {"reachable": True, "model": MODEL}})

    def test_닿지_않아도_오류가_아니라_reachable_false로_답한다(self):
        self.embedder.down = True
        res = self.client.get("/embeddings/health", headers=HEADERS)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"embedder": {"reachable": False, "model": None}})


class DeleteTest(ApiTestCase):
    def test_있으면_지우고_없어도_오류가_아니다(self):
        self.store.rows["p1"] = (MODEL, "h", [0.0])

        first = self.client.delete("/embeddings/posts/p1", headers=HEADERS)
        second = self.client.delete("/embeddings/posts/p1", headers=HEADERS)

        self.assertEqual((first.status_code, first.json()), (200, {"deleted": True, "jobDeleted": False}))
        self.assertEqual((second.status_code, second.json()), (200, {"deleted": False, "jobDeleted": False}))
        self.assertEqual(self.store.rows, {})

    def test_글을_삭제하면_대기_작업도_지운다(self):
        # 대기 작업에는 글 본문이 들어 있다 — 삭제한 글의 내용이 남으면 안 된다
        self.jobs.jobs["p1"] = {"title": "t", "content": "<p>비공개 내용</p>", "version": 1}
        self.jobs.jobs["p2"] = {"title": "다른 글", "content": "<p>x</p>", "version": 1}

        res = self.client.delete("/embeddings/posts/p1", headers=HEADERS)

        self.assertEqual(res.json(), {"deleted": False, "jobDeleted": True})
        self.assertEqual(list(self.jobs.jobs), ["p2"])

    def test_대기_작업을_지우지_못하면_숨기지_않고_오류로_알린다(self):
        self.jobs.fail_delete = True

        with self.assertRaises(RuntimeError):  # 실제 서버에서는 500이 된다
            self.client.delete("/embeddings/posts/p1", headers=HEADERS)


class SimilarTest(ApiTestCase):
    def post(self, body):
        return self.client.post("/embeddings/similar", json=body, headers=HEADERS)

    # --- 요청 검증 ---
    def test_기준은_postId_또는_텍스트_중_하나만(self):
        bad = [
            {"candidateIds": ["a"]},  # 기준 없음
            {"postId": "p1", "title": "t", "content": "c", "candidateIds": ["a"]},  # 둘 다
            {"title": "t", "candidateIds": ["a"]},  # 텍스트인데 content 없음
            {"content": "c", "candidateIds": ["a"]},  # 텍스트인데 title 없음
            {"postId": "", "candidateIds": ["a"]},
        ]
        for body in bad:
            with self.subTest(body=body):
                self.assertEqual(self.post(body).status_code, 422)
        self.assertEqual((self.embedder.calls, self.store.similar_calls), ([], []))

    def test_후보_id는_필수이고_상한과_범위를_지킨다(self):
        self.assertEqual(self.post({"postId": "p1"}).status_code, 422)  # candidateIds 없음
        self.assertEqual(self.post({"postId": "p1", "candidateIds": ["a"] * (embeddings.MAX_CANDIDATES + 1)}).status_code, 422)
        for extra in ({"limit": 0}, {"limit": embeddings.MAX_LIMIT + 1}, {"minScore": -0.1}, {"minScore": 1.5}):
            with self.subTest(extra=extra):
                self.assertEqual(self.post({"postId": "p1", "candidateIds": ["a"], **extra}).status_code, 422)

    # --- 후보가 비어 있을 때 ---
    def test_후보가_없으면_아무것도_조회하지_않고_빈_결과(self):
        for body in ({"postId": "p1", "candidateIds": []}, {"title": "t", "content": "c", "candidateIds": []}):
            with self.subTest(body=body):
                res = self.post(body)
                self.assertEqual((res.status_code, res.json()["items"]), (200, []))
        self.assertEqual((self.embedder.calls, self.store.similar_calls), ([], []))  # 임베딩 서비스도 저장소도 안 부른다

    # --- 저장된 글 기준 ---
    def test_postId_기준은_후보_안에서_조회한_결과를_점수와_함께_돌려준다(self):
        self.store.post_matches = [Match("b", 0.912345), Match("c", 0.7)]

        res = self.post({"postId": "p1", "candidateIds": ["b", "c", "d"], "limit": 2, "minScore": 0.6})

        self.assertEqual(res.json(), {"items": [{"postId": "b", "score": 0.9123}, {"postId": "c", "score": 0.7}], "sourceEmbedded": True})
        self.assertEqual(self.store.similar_calls, [("post", "p1", ["b", "c", "d"], 2, 0.6)])
        self.assertEqual(self.embedder.calls, [])  # 이미 저장된 벡터를 쓰므로 임베딩 서비스를 부르지 않는다

    def test_후보_id의_중복은_한_번만_넘긴다(self):
        self.post({"postId": "p1", "candidateIds": ["b", "c", "b", "c", "d"]})

        self.assertEqual(self.store.similar_calls[0][2], ["b", "c", "d"])

    def test_기준_글의_벡터가_없으면_빈_결과와_sourceEmbedded_false(self):
        self.store.post_matches = None

        res = self.post({"postId": "p1", "candidateIds": ["b"]})

        self.assertEqual(res.json(), {"items": [], "sourceEmbedded": False})

    def test_기본_limit은_5_minScore는_0(self):
        self.post({"postId": "p1", "candidateIds": ["b"]})

        self.assertEqual(self.store.similar_calls[0][3:], (5, 0.0))

    # --- 저장 전 텍스트 기준 (쓰는 중인 글) ---
    def test_텍스트_기준은_임베딩해서_후보_안에서_조회한다(self):
        self.store.vector_matches = [Match("b", 0.8)]

        res = self.post({"title": "도커", "content": "<p>컨테이너</p>", "candidateIds": ["b", "c"]})

        text = build_embedding_text("도커", html_to_text("<p>컨테이너</p>"))
        self.assertEqual(self.embedder.calls, [[text]])
        self.assertEqual(self.store.similar_calls, [("vector", [float(len(text))], ["b", "c"], 5, 0.0)])
        self.assertEqual(res.json(), {"items": [{"postId": "b", "score": 0.8}], "sourceEmbedded": None})

    def test_텍스트_기준에서_내용이_비면_422(self):
        res = self.post({"title": " ", "content": "<p></p>", "candidateIds": ["b"]})

        self.assertEqual(res.status_code, 422)
        self.assertEqual(self.embedder.calls, [])

    def test_텍스트_기준에서_임베딩_서비스를_쓸_수_없으면_503(self):
        self.embedder.down = True

        res = self.post({"title": "t", "content": "c", "candidateIds": ["b"]})

        self.assertEqual(res.status_code, 503)
        self.assertEqual(self.store.similar_calls, [])


if __name__ == "__main__":
    unittest.main()
