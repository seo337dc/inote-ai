"""EmbeddingJobStore를 실제 DB로 확인하는 통합 테스트 — 특히 "같은 글은 행 하나", "다시 저장하면 pending으로 되돌아간다".

TEST_DATABASE_URL이 있을 때만 돈다 (없으면 건너뜀). 로컬 개발 DB를 쓴다 (schema.sql이 적용돼 있어야 한다):
  TEST_DATABASE_URL="postgresql://ai:<AI_DEV_DB_PASSWORD>@127.0.0.1:5434/inote_ai_dev" python -m unittest tests.test_embedding_job_db -v
테스트가 만든 행(post_id가 'itest-job-'로 시작)은 끝나면 지운다. 운영 DB로 돌리지 않는다.
"""

import os
import unittest
from unittest import mock

import psycopg

from app.services.embedding_jobs import EmbeddingJobStore
from app.services.embedding_store import upsert_with_cursor

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
DIM = 1024


@unittest.skipUnless(TEST_DATABASE_URL, "TEST_DATABASE_URL이 없어 실제 DB 테스트를 건너뜁니다")
class EmbeddingJobStoreDbTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"DATABASE_URL": TEST_DATABASE_URL})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.cleanup)
        self.cleanup()
        self.jobs = EmbeddingJobStore()

    @staticmethod
    def cleanup():
        with psycopg.connect(TEST_DATABASE_URL) as conn, conn.cursor() as cur:
            cur.execute("delete from embedding_job where post_id like 'itest-job-%'")

    @staticmethod
    def row(post_id):
        with psycopg.connect(TEST_DATABASE_URL) as conn, conn.cursor() as cur:
            cur.execute(
                "select title, content, status, attempts, last_error, version from embedding_job where post_id = %s",
                (post_id,),
            )
            return cur.fetchone()

    @staticmethod
    def count(post_id):
        with psycopg.connect(TEST_DATABASE_URL) as conn, conn.cursor() as cur:
            cur.execute("select count(*) from embedding_job where post_id = %s", (post_id,))
            return cur.fetchone()[0]

    def test_새_작업은_pending_version_1로_등록된다(self):
        version = self.jobs.enqueue("itest-job-1", "제목", "<p>본문</p>")

        self.assertEqual(version, 1)
        self.assertEqual(self.row("itest-job-1"), ("제목", "<p>본문</p>", "pending", 0, None, 1))

    def test_같은_글을_다시_등록하면_행은_하나이고_내용과_version이_갱신된다(self):
        self.jobs.enqueue("itest-job-1", "옛 제목", "<p>옛 내용</p>")

        version = self.jobs.enqueue("itest-job-1", "새 제목", "<p>새 내용</p>")

        self.assertEqual(version, 2)
        self.assertEqual(self.count("itest-job-1"), 1)
        self.assertEqual(self.row("itest-job-1"), ("새 제목", "<p>새 내용</p>", "pending", 0, None, 2))

    def test_실패했거나_끝난_작업도_다시_등록하면_pending으로_되돌아간다(self):
        for status, attempts, error, content in (("failed", 5, "x", "<p>옛</p>"), ("done", 1, None, None), ("processing", 1, None, "<p>옛</p>")):
            with self.subTest(status=status):
                self.jobs.enqueue("itest-job-1", "t", "<p>c</p>")
                with psycopg.connect(TEST_DATABASE_URL) as conn, conn.cursor() as cur:
                    cur.execute(
                        "update embedding_job set status = %s, attempts = %s, last_error = %s, content = %s where post_id = 'itest-job-1'",
                        (status, attempts, error, content),
                    )

                self.jobs.enqueue("itest-job-1", "새 제목", "<p>새 내용</p>")

                title, new_content, new_status, new_attempts, last_error, _ = self.row("itest-job-1")
                self.assertEqual((title, new_content, new_status, new_attempts, last_error), ("새 제목", "<p>새 내용</p>", "pending", 0, None))

    def test_다른_글의_작업은_영향을_받지_않는다(self):
        self.jobs.enqueue("itest-job-1", "하나", "<p>1</p>")
        self.jobs.enqueue("itest-job-2", "둘", "<p>2</p>")

        self.jobs.enqueue("itest-job-1", "하나 수정", "<p>1-수정</p>")
        self.jobs.delete("itest-job-1")

        self.assertEqual(self.row("itest-job-2"), ("둘", "<p>2</p>", "pending", 0, None, 1))

    def test_삭제하면_지우고_없어도_오류가_아니다(self):
        self.jobs.enqueue("itest-job-1", "t", "<p>c</p>")

        self.assertTrue(self.jobs.delete("itest-job-1"))
        self.assertFalse(self.jobs.delete("itest-job-1"))
        self.assertEqual(self.count("itest-job-1"), 0)


def sql(query, params=None):
    # params가 None이어야 쿼리 안의 LIKE 'itest-job-%'의 %가 자리표시자로 해석되지 않는다
    with psycopg.connect(TEST_DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall() if cur.description else None


def one_hot():
    return [1.0] + [0.0] * (DIM - 1)


@unittest.skipUnless(TEST_DATABASE_URL, "TEST_DATABASE_URL이 없어 실제 DB 테스트를 건너뜁니다")
class EmbeddingJobQueueDbTest(unittest.TestCase):
    """워커가 쓰는 SQL: 가져오기(SKIP LOCKED·정체 복구), 한 트랜잭션 완료, 실패·되돌리기·정리."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"DATABASE_URL": TEST_DATABASE_URL})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.cleanup)
        self.cleanup()
        # claim_next는 DB 전체에서 가장 오래된 작업을 가져간다 — 개발 DB에 시험용이 아닌 대기 작업이 있으면 건드리지 않도록 건너뛴다
        others = sql("select count(*) from embedding_job where post_id not like 'itest-job-%' and status in ('pending', 'processing')")[0][0]
        if others:
            self.skipTest(f"개발 DB에 시험용이 아닌 대기·처리 중 작업이 {others}건 있어 건너뜁니다")
        self.jobs = EmbeddingJobStore()

    @staticmethod
    def cleanup():
        sql("delete from embedding_job where post_id like 'itest-job-%'")
        sql("delete from post_embedding where post_id like 'itest-job-%'")

    @staticmethod
    def seed(post_id, status="pending", attempts=0, age_seconds=3600, content="<p>내용</p>", title="제목"):
        sql(
            "insert into embedding_job (post_id, title, content, status, attempts, updated_at) "
            "values (%s, %s, %s, %s, %s, now() - make_interval(secs => %s))",
            (post_id, title, content, status, attempts, float(age_seconds)),
        )

    @staticmethod
    def row(post_id):
        rows = sql("select status, attempts, version, content, last_error from embedding_job where post_id = %s", (post_id,))
        return rows[0] if rows else None

    @staticmethod
    def vector_exists(post_id):
        return sql("select count(*) from post_embedding where post_id = %s", (post_id,))[0][0] == 1

    # --- 가져오기 ---
    def test_오래된_순으로_하나씩_가져오고_processing으로_바꾸며_시도_횟수를_올린다(self):
        self.seed("itest-job-a", age_seconds=3000, title="첫째", content="<p>a</p>")
        self.seed("itest-job-b", age_seconds=2000)
        self.seed("itest-job-c", age_seconds=1000)

        first = self.jobs.claim_next(5, 600)

        self.assertEqual((first.post_id, first.title, first.content, first.version, first.attempts), ("itest-job-a", "첫째", "<p>a</p>", 1, 1))
        self.assertEqual(self.row("itest-job-a")[:2], ("processing", 1))
        self.assertEqual(self.jobs.claim_next(5, 600).post_id, "itest-job-b")  # 이미 가져간 a는 다시 나오지 않는다
        self.assertEqual(self.jobs.claim_next(5, 600).post_id, "itest-job-c")
        self.assertIsNone(self.jobs.claim_next(5, 600))

    def test_시도_횟수가_상한에_닿은_작업은_가져오지_않는다(self):
        self.seed("itest-job-a", attempts=5)

        self.assertIsNone(self.jobs.claim_next(5, 600))
        self.assertEqual(self.jobs.claim_next(6, 600).post_id, "itest-job-a")  # 상한이 늘면 가져온다

    def test_done과_failed는_가져오지_않는다(self):
        self.seed("itest-job-a", status="done", content=None)
        self.seed("itest-job-b", status="failed")

        self.assertIsNone(self.jobs.claim_next(5, 600))

    def test_다른_트랜잭션이_잡고_있는_작업은_건너뛰고_다음_작업을_가져간다(self):
        # SKIP LOCKED: 워커가 둘이어도 같은 작업을 동시에 가져가지 않는다
        self.seed("itest-job-a", age_seconds=3000)
        self.seed("itest-job-b", age_seconds=2000)

        with psycopg.connect(TEST_DATABASE_URL) as other:
            other.execute("select 1 from embedding_job where post_id = 'itest-job-a' for update")  # 다른 워커가 a를 잡고 있다

            claimed = self.jobs.claim_next(5, 600)

        self.assertEqual(claimed.post_id, "itest-job-b")  # a를 기다리지 않고 건너뛴다
        self.assertEqual(self.jobs.claim_next(5, 600).post_id, "itest-job-a")  # 잠금이 풀린 뒤에는 a도 가져갈 수 있다

    def test_처리_중인_채_오래_갱신되지_않은_작업은_다시_가져가고_최근_것은_건드리지_않는다(self):
        self.seed("itest-job-stale", status="processing", attempts=1, age_seconds=3600)  # 워커가 중간에 멈춘 작업
        self.seed("itest-job-fresh", status="processing", attempts=1, age_seconds=1)  # 지금 다른 워커가 처리 중

        claimed = self.jobs.claim_next(5, 600)

        self.assertEqual((claimed.post_id, claimed.attempts), ("itest-job-stale", 2))
        self.assertIsNone(self.jobs.claim_next(5, 600))
        self.assertEqual(self.row("itest-job-fresh")[:2], ("processing", 1))

    # --- 완료 ---
    def test_완료는_벡터_저장과_done_처리를_한_번에_하고_본문을_비운다(self):
        self.seed("itest-job-a")
        job = self.jobs.claim_next(5, 600)

        applied = self.jobs.complete(job.post_id, job.version, lambda cur: upsert_with_cursor(cur, "itest-job-a", "m", "h", one_hot()))

        self.assertTrue(applied)
        self.assertTrue(self.vector_exists("itest-job-a"))
        self.assertEqual(self.row("itest-job-a"), ("done", 1, 1, None, None))  # 본문은 비워진다

    def test_처리_중에_글이_다시_저장돼_version이_달라졌으면_벡터를_쓰지_않는다(self):
        self.seed("itest-job-a")
        job = self.jobs.claim_next(5, 600)
        self.jobs.enqueue("itest-job-a", "새 제목", "<p>새 내용</p>")  # 처리 중에 글이 수정됨 → version 2, pending

        applied = self.jobs.complete(job.post_id, job.version, lambda cur: upsert_with_cursor(cur, "itest-job-a", "m", "h", one_hot()))

        self.assertFalse(applied)
        self.assertFalse(self.vector_exists("itest-job-a"))  # 오래된 내용의 벡터는 쓰지 않는다
        self.assertEqual(self.row("itest-job-a")[:4], ("pending", 0, 2, "<p>새 내용</p>"))  # 새 내용의 작업은 그대로 대기

    def test_처리_중에_글이_삭제됐으면_벡터를_쓰지_않는다(self):
        self.seed("itest-job-a")
        job = self.jobs.claim_next(5, 600)
        self.jobs.delete("itest-job-a")

        applied = self.jobs.complete(job.post_id, job.version, lambda cur: upsert_with_cursor(cur, "itest-job-a", "m", "h", one_hot()))

        self.assertFalse(applied)
        self.assertFalse(self.vector_exists("itest-job-a"))

    def test_processing이_아닌_작업은_완료하지_않는다(self):
        self.seed("itest-job-a", status="pending")

        applied = self.jobs.complete("itest-job-a", 1, lambda cur: upsert_with_cursor(cur, "itest-job-a", "m", "h", one_hot()))

        self.assertFalse(applied)
        self.assertFalse(self.vector_exists("itest-job-a"))

    def test_벡터_저장이_실패하면_완료_처리도_함께_되돌아간다(self):
        self.seed("itest-job-a")
        job = self.jobs.claim_next(5, 600)

        def broken(cur):
            raise RuntimeError("저장 실패")

        with self.assertRaises(RuntimeError):
            self.jobs.complete(job.post_id, job.version, broken)

        self.assertEqual(self.row("itest-job-a")[:2], ("processing", 1))  # done으로 바뀌지 않았다
        self.assertFalse(self.vector_exists("itest-job-a"))

    # --- 실패·되돌리기·정리 ---
    def test_실패는_상한_전이면_pending_상한이면_failed_영구_실패는_바로_failed(self):
        for attempts, permanent, expected in ((2, False, "pending"), (5, False, "failed"), (1, True, "failed")):
            with self.subTest(attempts=attempts, permanent=permanent):
                self.cleanup()
                self.seed("itest-job-a", status="processing", attempts=attempts)

                status = self.jobs.fail("itest-job-a", 1, "에러 내용", 5, permanent=permanent)

                self.assertEqual(status, expected)
                self.assertEqual(self.row("itest-job-a")[0], expected)
                self.assertEqual(self.row("itest-job-a")[4], "에러 내용")  # 마지막 에러가 남는다

    def test_실패_기록은_version이_다르거나_processing이_아니면_건드리지_않는다(self):
        self.seed("itest-job-a", status="processing", attempts=1)
        self.seed("itest-job-b", status="pending")

        self.assertIsNone(self.jobs.fail("itest-job-a", 99, "x", 5))  # 그 사이 글이 다시 저장돼 version이 다름
        self.assertIsNone(self.jobs.fail("itest-job-b", 1, "x", 5))
        self.assertEqual(self.row("itest-job-a")[0], "processing")
        self.assertEqual(self.row("itest-job-b")[0], "pending")

    def test_너무_긴_에러는_잘라서_저장한다(self):
        self.seed("itest-job-a", status="processing", attempts=1)

        self.jobs.fail("itest-job-a", 1, "가" * 2000, 5)

        self.assertEqual(len(self.row("itest-job-a")[4]), 500)

    def test_되돌리기는_pending으로_두고_시도_횟수를_하나_줄인다(self):
        self.seed("itest-job-a", status="processing", attempts=2)
        self.seed("itest-job-b", status="processing", attempts=0)

        self.assertTrue(self.jobs.release("itest-job-a", 1, "서비스에 닿지 못함"))
        self.assertTrue(self.jobs.release("itest-job-b", 1, "서비스에 닿지 못함"))

        self.assertEqual(self.row("itest-job-a")[:2], ("pending", 1))
        self.assertEqual(self.row("itest-job-b")[:2], ("pending", 0))  # 0 아래로 내려가지 않는다
        self.assertEqual(self.row("itest-job-a")[4], "서비스에 닿지 못함")

    def test_멈춘_워커의_작업_중_상한에_닿은_것만_failed로_표시한다(self):
        self.seed("itest-job-stale-max", status="processing", attempts=5, age_seconds=3600)  # 멈췄고 상한 도달 → failed
        self.seed("itest-job-stale-left", status="processing", attempts=1, age_seconds=3600)  # 멈췄지만 횟수 남음 → 다시 가져갈 수 있다
        self.seed("itest-job-fresh-max", status="processing", attempts=5, age_seconds=1)  # 지금 처리 중 → 건드리지 않는다

        changed = self.jobs.fail_abandoned(5, 600)

        self.assertEqual(changed, 1)
        self.assertEqual(self.row("itest-job-stale-max")[0], "failed")
        self.assertEqual(self.row("itest-job-stale-left")[0], "processing")
        self.assertEqual(self.row("itest-job-fresh-max")[0], "processing")

    def test_failed를_다시_시도하도록_되돌린다(self):
        self.seed("itest-job-a", status="failed", attempts=5)
        sql("update embedding_job set last_error = '이전 에러' where post_id = 'itest-job-a'")
        self.seed("itest-job-b", status="failed", attempts=5, content=None)  # 본문이 없으면 다시 처리할 수 없다

        changed = self.jobs.requeue_failed()

        self.assertEqual(changed, 1)
        self.assertEqual(self.row("itest-job-a")[:2], ("pending", 0))
        self.assertIsNone(self.row("itest-job-a")[4])
        self.assertEqual(self.row("itest-job-b")[0], "failed")

    def test_상태별_개수(self):
        before = self.jobs.counts()
        self.seed("itest-job-a", status="pending")
        self.seed("itest-job-b", status="pending")
        self.seed("itest-job-c", status="failed")

        after = self.jobs.counts()

        self.assertEqual(after.get("pending", 0) - before.get("pending", 0), 2)
        self.assertEqual(after.get("failed", 0) - before.get("failed", 0), 1)


if __name__ == "__main__":
    unittest.main()
