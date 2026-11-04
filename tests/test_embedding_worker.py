"""임베딩 워커 로직 테스트 — 가짜 대기열·가짜 저장소·가짜 임베딩 서비스로 돈다 (DB·네트워크 없음).
실제 SQL(SKIP LOCKED, version 확인, 한 트랜잭션 완료 등)은 tests/test_embedding_job_db.py(실제 DB)가 확인한다."""

import os
import threading
import unittest
from typing import List
from unittest import mock

from app.services.embedder_client import EXPECTED_DIM, EmbedderUnavailable, EmbedResult, expected_model
from app.services.embedding_jobs import Job
from app.services.embedding_worker import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_STALE_SECONDS,
    EmbeddingWorker,
    RunSummary,
    WorkerConfig,
)
from app.services.text_prep import build_embedding_text, content_hash, html_to_text

MODEL = "nlpai-lab/KURE-v1"


def job(post_id, title="제목", content="<p>본문</p>", version=1, attempts=1):
    return Job(post_id=post_id, title=title, content=content, version=version, attempts=attempts)


class FakeCursor:
    def __init__(self):
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))


class FakeJobs:
    """대기열 호출 기록용. 큐에서 하나씩 꺼내 주고, 결과 기록(complete/fail/release)을 남긴다."""

    def __init__(self, jobs: List[Job]):
        self.queue = list(jobs)
        self.claim_args = []
        self.completed = []  # (post_id, version)
        self.failed = []  # (post_id, error, permanent)
        self.released = []  # (post_id, note)
        self.abandoned = 0
        self.refuse_complete = set()  # 처리 중에 글이 바뀐 것처럼 complete를 거부할 글
        self.cursor = FakeCursor()
        self.max_attempts_seen = None

    def fail_abandoned(self, max_attempts, stale_seconds):
        return self.abandoned

    def claim_next(self, max_attempts, stale_seconds):
        self.claim_args.append((max_attempts, stale_seconds))
        self.max_attempts_seen = max_attempts
        return self.queue.pop(0) if self.queue else None

    def complete(self, post_id, version, write_vector):
        if post_id in self.refuse_complete:
            return False
        write_vector(self.cursor)
        self.completed.append((post_id, version))
        return True

    def fail(self, post_id, version, error, max_attempts, permanent=False):
        self.failed.append((post_id, error, permanent))
        attempts = self._attempts.get(post_id, 1)
        return "failed" if permanent or attempts >= max_attempts else "pending"

    def release(self, post_id, version, note):
        self.released.append((post_id, note))
        return True

    _attempts = {}


class FakeStore:
    def __init__(self):
        self.states = {}

    def get_state(self, post_id):
        return self.states.get(post_id)


class FakeEmbedder:
    def __init__(self):
        self.calls: List[List[str]] = []
        self.error_for = {}  # 텍스트에 이 문자열이 들어 있으면 해당 예외를 던진다
        self.always = None  # 모든 호출에 던질 예외

    def embed(self, texts):
        self.calls.append(list(texts))
        if self.always is not None:
            raise self.always
        for needle, error in self.error_for.items():
            if needle in texts[0]:
                raise error
        return EmbedResult(model=MODEL, dim=EXPECTED_DIM, embeddings=[[float(len(t))] + [0.0] * (EXPECTED_DIM - 1) for t in texts])


def unreachable(message="연결 실패"):
    return EmbedderUnavailable(message, unreachable=True)


def responded_with_error(message="500을 돌려줬습니다"):
    return EmbedderUnavailable(message)


class WorkerTestCase(unittest.TestCase):
    def build(self, jobs, **config):
        self.jobs, self.store, self.embedder = FakeJobs(jobs), FakeStore(), FakeEmbedder()
        self.jobs._attempts = {j.post_id: j.attempts for j in jobs}
        self.worker = EmbeddingWorker(self.jobs, self.store, self.embedder, WorkerConfig(**config) if config else WorkerConfig())
        return self.worker


class ProcessingTest(WorkerTestCase):
    def test_대기_작업을_하나씩_임베딩하고_벡터를_저장하고_완료한다(self):
        worker = self.build([job("a", "도커", "<h1>시작</h1><p>컨테이너</p>"), job("b", "쿠버", "<p>파드</p>")])

        summary = worker.run_once()

        self.assertEqual((summary.done, summary.handled, summary.stopped), (2, 2, None))
        self.assertEqual(self.jobs.completed, [("a", 1), ("b", 1)])
        expected_text = build_embedding_text("도커", html_to_text("<h1>시작</h1><p>컨테이너</p>"))
        self.assertEqual(self.embedder.calls[0], [expected_text])  # HTML 태그는 지워서 보낸다
        sql, params = self.jobs.cursor.executed[0]
        self.assertIn("post_embedding", sql)
        self.assertEqual(params[:3], ("a", MODEL, content_hash(expected_text)))  # 벡터를 같은 트랜잭션(cursor)으로 저장

    def test_대기열이_비어_있으면_아무것도_하지_않고_끝난다(self):
        summary = self.build([]).run_once()

        self.assertEqual((summary.handled, summary.stopped, summary.has_problem), (0, None, False))
        self.assertEqual(self.embedder.calls, [])

    def test_설정한_시도_상한과_정체_시간을_대기열에_넘긴다(self):
        worker = self.build([job("a")], max_attempts=7, stale_seconds=123.0)

        worker.run_once()

        self.assertEqual(self.jobs.claim_args[0], (7, 123.0))

    def test_같은_내용과_모델의_벡터가_이미_있으면_임베딩_서비스를_부르지_않고_완료한다(self):
        worker = self.build([job("a", "제목", "<p>본문</p>")])
        self.store.states["a"] = (expected_model(), content_hash(build_embedding_text("제목", html_to_text("<p>본문</p>"))))

        summary = worker.run_once()

        self.assertEqual((summary.unchanged, summary.done), (1, 0))
        self.assertEqual(self.embedder.calls, [])
        self.assertEqual(self.jobs.completed, [("a", 1)])

    def test_모델이_다르면_해시가_같아도_다시_임베딩한다(self):
        worker = self.build([job("a", "제목", "<p>본문</p>")])
        self.store.states["a"] = ("old/model", content_hash(build_embedding_text("제목", html_to_text("<p>본문</p>"))))

        summary = worker.run_once()

        self.assertEqual(summary.done, 1)
        self.assertEqual(len(self.embedder.calls), 1)

    def test_처리_중에_글이_바뀌어_완료가_거부되면_이번_결과는_버린다(self):
        worker = self.build([job("a")])
        self.jobs.refuse_complete.add("a")

        summary = worker.run_once()

        self.assertEqual((summary.superseded, summary.done, summary.failed), (1, 0, 0))

    def test_멈춘_워커의_작업_정리_개수가_요약에_담기고_문제로_본다(self):
        worker = self.build([])
        self.jobs.abandoned = 2

        summary = worker.run_once()

        self.assertEqual(summary.abandoned, 2)
        self.assertTrue(summary.has_problem)


class FailureTest(WorkerTestCase):
    def test_서비스에_닿지_못하면_시도_횟수를_세지_않고_되돌리고_멈춘다(self):
        worker = self.build([job("a"), job("b"), job("c")])
        self.embedder.always = unreachable("연결 실패")

        summary = worker.run_once()

        self.assertEqual([r[0] for r in self.jobs.released], ["a"])  # 첫 작업만 되돌리고
        self.assertEqual(self.jobs.failed, [])  # 실패로 기록하지 않는다(시도 횟수를 세지 않는다)
        self.assertEqual([j.post_id for j in self.jobs.queue], ["b", "c"])  # 나머지는 가져가지도 않았다
        self.assertIn("연결할 수 없어", summary.stopped)
        self.assertTrue(summary.has_problem)

    def test_응답은_왔지만_실패하면_시도_횟수가_남아_있는_동안_재시도_대기로_둔다(self):
        worker = self.build([job("a", attempts=2)])
        self.embedder.always = responded_with_error("임베딩 서비스가 500을 돌려줬습니다.")

        summary = worker.run_once()

        self.assertEqual((summary.retried, summary.failed), (1, 0))
        self.assertEqual(self.jobs.failed[0][0], "a")
        self.assertIn("500", self.jobs.failed[0][1])  # 마지막 에러가 기록된다
        self.assertFalse(self.jobs.failed[0][2])  # 영구 실패가 아님

    def test_시도_횟수가_상한에_닿으면_failed로_둔다(self):
        worker = self.build([job("a", attempts=DEFAULT_MAX_ATTEMPTS)])
        self.embedder.always = responded_with_error()

        summary = worker.run_once()

        self.assertEqual((summary.failed, summary.retried), (1, 0))

    def test_한_작업이_계속_실패해도_큐를_막지_않고_다음_작업을_처리한다(self):
        worker = self.build([job("poison", "독이 든 글", "<p>POISON</p>", attempts=DEFAULT_MAX_ATTEMPTS), job("ok", "정상 글", "<p>fine</p>")])
        self.embedder.error_for["POISON"] = responded_with_error()

        summary = worker.run_once()

        self.assertEqual((summary.failed, summary.done), (1, 1))
        self.assertEqual(self.jobs.completed, [("ok", 1)])

    def test_연속으로_여러_건_실패하면_서비스_문제로_보고_멈춘다(self):
        worker = self.build([job(str(i)) for i in range(5)])
        self.embedder.always = responded_with_error()

        summary = worker.run_once()

        self.assertEqual(len(self.jobs.failed), 3)  # 기본 3건에서 멈춘다
        self.assertEqual(len(self.jobs.queue), 2)  # 나머지 2건은 시도 횟수를 쓰지 않고 남는다
        self.assertIn("연속 3건 실패", summary.stopped)

    def test_중간에_성공이_끼면_연속_실패_횟수가_초기화된다(self):
        jobs = [job("f1", content="<p>BAD</p>"), job("f2", content="<p>BAD</p>"), job("ok"), job("f3", content="<p>BAD</p>"), job("f4", content="<p>BAD</p>")]
        worker = self.build(jobs)
        self.embedder.error_for["BAD"] = responded_with_error()

        summary = worker.run_once()

        self.assertEqual((summary.done, summary.retried, summary.stopped), (1, 4, None))
        self.assertEqual(self.jobs.queue, [])

    def test_임베딩할_내용이_없으면_서비스를_부르지_않고_바로_failed로_둔다(self):
        worker = self.build([job("a", title="", content="<p> </p>")])

        summary = worker.run_once()

        self.assertEqual((summary.failed, summary.retried), (1, 0))
        self.assertEqual(self.jobs.failed[0], ("a", "임베딩할 내용이 없습니다.", True))  # permanent
        self.assertEqual(self.embedder.calls, [])

    def test_내용이_빈_작업이_여러_개여도_서비스_문제로_보고_멈추지_않는다(self):
        worker = self.build([job(str(i), title="", content="") for i in range(5)])

        summary = worker.run_once()

        self.assertEqual((summary.failed, summary.stopped), (5, None))  # 글 내용의 문제라 연속 실패로 세지 않는다

    def test_예상_못_한_오류는_종류만_기록한다(self):
        worker = self.build([job("a", content="<p>SECRET</p>")])
        self.embedder.error_for["SECRET"] = ValueError("비공개 본문 일부가 섞인 오류 메시지")

        worker.run_once()

        self.assertEqual(self.jobs.failed[0][1], "ValueError")  # 메시지(값이 섞일 수 있음)는 남기지 않는다

    def test_저장_단계의_DB_오류도_실패로_기록한다(self):
        worker = self.build([job("a")])
        self.jobs.complete = mock.Mock(side_effect=RuntimeError("db down"))

        summary = worker.run_once()

        self.assertEqual(summary.retried, 1)
        self.assertEqual(self.jobs.failed[0][1], "RuntimeError")


class LoopAndStopTest(WorkerTestCase):
    def test_중단_요청이_서면_작업을_가져오지_않고_멈춘다(self):
        worker = self.build([job("a")])
        worker.stop_event.set()

        summary = worker.run_once()

        self.assertEqual(self.jobs.claim_args, [])
        self.assertIn("중단 요청", summary.stopped)

    def test_작업_도중_중단_요청이_서면_지금_작업까지만_끝낸다(self):
        worker = self.build([job("a"), job("b")])
        original = self.embedder.embed
        self.embedder.embed = lambda texts: (worker.stop_event.set(), original(texts))[1]  # 첫 작업 중에 Ctrl+C

        summary = worker.run_once()

        self.assertEqual(self.jobs.completed, [("a", 1)])  # a는 끝까지 처리
        self.assertEqual([j.post_id for j in self.jobs.queue], ["b"])  # b는 건드리지 않음

    def test_반복_실행은_중단될_때까지_주기적으로_돌고_요약을_전달한다(self):
        worker = self.build([job("a")])
        seen = []

        def on_summary(summary):
            seen.append(summary)
            if len(seen) == 2:
                worker.stop_event.set()

        worker.run_loop(0, on_summary=on_summary)

        self.assertEqual(len(seen), 2)
        self.assertEqual([s.done for s in seen], [1, 0])  # 첫 주기에 처리, 다음 주기는 빈 큐

    def test_반복_실행은_서비스가_꺼져_있어도_끝나지_않고_다음_주기에_다시_시도한다(self):
        worker = self.build([job("a")])
        self.embedder.always = unreachable()
        runs = []

        def on_summary(summary):
            runs.append(summary.stopped)
            if len(runs) == 2:
                worker.stop_event.set()

        # release된 작업이 다음 주기에 다시 나오도록, 되돌려진 작업을 큐 뒤에 다시 넣는 가짜 동작
        original_release = self.jobs.release
        self.jobs.release = lambda pid, v, note: (self.jobs.queue.append(job(pid)), original_release(pid, v, note))[1]

        worker.run_loop(0, on_summary=on_summary)

        self.assertEqual(len(runs), 2)
        self.assertTrue(all("연결할 수 없어" in r for r in runs))


class LogTest(WorkerTestCase):
    def test_로그에는_글_id만_남기고_제목과_본문은_남기지_않는다(self):
        worker = self.build([job("post-1", "비밀 제목", "<p>비공개 본문</p>"), job("post-2", "또 다른 제목", "<p>BAD 본문</p>")])
        self.embedder.error_for["BAD"] = responded_with_error()

        with self.assertLogs("app.services.embedding_worker", level="INFO") as logs:
            worker.run_once()

        text = "\n".join(logs.output)
        self.assertIn("post-1", text)
        for secret in ("비밀 제목", "비공개 본문", "또 다른 제목", "BAD 본문"):
            self.assertNotIn(secret, text)


class ConfigTest(unittest.TestCase):
    def test_기본값(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            config = WorkerConfig.from_env()
        self.assertEqual((config.max_attempts, config.stale_seconds), (DEFAULT_MAX_ATTEMPTS, DEFAULT_STALE_SECONDS))

    def test_환경_변수로_바꿀_수_있다(self):
        with mock.patch.dict(os.environ, {"EMBEDDING_JOB_MAX_ATTEMPTS": "3", "EMBEDDING_JOB_STALE_SECONDS": "120"}, clear=True):
            config = WorkerConfig.from_env()
        self.assertEqual((config.max_attempts, config.stale_seconds), (3, 120.0))

    def test_잘못되었거나_범위를_벗어나면_기본값(self):
        for name, bad in (("EMBEDDING_JOB_MAX_ATTEMPTS", "0"), ("EMBEDDING_JOB_MAX_ATTEMPTS", "21"), ("EMBEDDING_JOB_MAX_ATTEMPTS", "abc"),
                          ("EMBEDDING_JOB_STALE_SECONDS", "5"), ("EMBEDDING_JOB_STALE_SECONDS", "x")):
            with self.subTest(name=name, bad=bad), mock.patch.dict(os.environ, {name: bad}, clear=True):
                config = WorkerConfig.from_env()
                self.assertEqual((config.max_attempts, config.stale_seconds), (DEFAULT_MAX_ATTEMPTS, DEFAULT_STALE_SECONDS))


class SummaryTest(unittest.TestCase):
    def test_처리_건수는_결과별_합이고_문제_여부는_실패_정지_정리_기준이다(self):
        self.assertEqual(RunSummary(done=2, unchanged=1, superseded=1, retried=1, failed=1).handled, 6)
        self.assertFalse(RunSummary(done=3, retried=1).has_problem)  # 재시도 대기만 있으면 문제는 아니다
        self.assertTrue(RunSummary(failed=1).has_problem)
        self.assertTrue(RunSummary(stopped="멈춤").has_problem)


if __name__ == "__main__":
    unittest.main()
