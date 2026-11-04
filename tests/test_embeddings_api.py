"""임베딩 API 테스트 — 가짜 저장소·가짜 임베딩 서비스로 돈다 (DB·네트워크 없음).
후보 id 제한이 SQL에서 실제로 지켜지는지는 tests/test_embedding_store_db.py(실제 pgvector)가 확인한다."""

import os
import unittest
from typing import List, Optional
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import embeddings
from app.services.embedder_client import EXPECTED_DIM, EmbedderClient, EmbedderUnavailable, EmbedResult, expected_model, get_embedder
from app.services.embedding_jobs import get_job_store
from app.services.embedding_store import Match, get_store
from app.services.text_prep import build_embedding_text, content_hash, html_to_text

SECRET = "test-secret"
HEADERS = {"x-internal-secret": SECRET}
MODEL = "nlpai-lab/KURE-v1"


class FakeEmbedder:
    def __init__(self):
        self.calls: List[List[str]] = []
        self.down = False

    def embed(self, texts):
        self.calls.append(list(texts))
        if self.down:
            raise EmbedderUnavailable("임베딩 서비스를 쓸 수 없습니다")
        return EmbedResult(model=MODEL, dim=EXPECTED_DIM, embeddings=[[float(len(t))] + [0.0] * (EXPECTED_DIM - 1) for t in texts])


class FakeStore:
    """저장소 호출 기록용. similar_*의 결과는 테스트가 정해 준다."""

    def __init__(self):
        self.rows = {}
        self.upserts = []
        self.similar_calls = []
        self.post_matches: Optional[List[Match]] = []
        self.vector_matches: List[Match] = []

    def get_state(self, post_id):
        row = self.rows.get(post_id)
        return (row[0], row[1]) if row else None

    def upsert(self, post_id, model, digest, vector):
        self.rows[post_id] = (model, digest, list(vector))
        self.upserts.append(post_id)

    def delete(self, post_id):
        return self.rows.pop(post_id, None) is not None

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
        self.assertEqual(res.json(), {"status": "embedded", "model": MODEL, "dim": EXPECTED_DIM})
        expected_text = build_embedding_text("도커", html_to_text("<h1>시작</h1><p>컨테이너</p>"))
        self.assertEqual(self.embedder.calls, [[expected_text]])  # HTML 태그는 지워서 보낸다
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
