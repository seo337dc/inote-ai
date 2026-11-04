"""조각(post_chunk_embedding) 저장을 실제 pgvector DB로 확인하는 통합 테스트 — 특히 "글 벡터와 조각은 한 트랜잭션이라 일부만 바뀌지 않는다".

TEST_DATABASE_URL이 있을 때만 돈다 (없으면 건너뜀). 로컬 개발 DB를 쓴다:
  TEST_DATABASE_URL="postgresql://ai:<AI_DEV_DB_PASSWORD>@127.0.0.1:5434/inote_ai_dev" python -m unittest tests.test_post_chunk_db -v
테스트가 만든 행(post_id가 'itest-chunk-'로 시작)은 끝나면 지운다. 운영 DB로 돌리지 않는다.
"""

import os
import unittest
from unittest import mock

import psycopg

from app.services.embedding_store import ChunkRow, EmbeddingStore

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
DIM = 1024
PID = "itest-chunk-1"


def vec(i: int):
    v = [0.0] * DIM
    v[i] = 1.0
    return v


def row(idx: int, start: int, end: int, i: int) -> ChunkRow:
    return ChunkRow(idx=idx, start=start, end=end, digest=f"h{idx}", vector=vec(i))


@unittest.skipUnless(TEST_DATABASE_URL, "TEST_DATABASE_URL이 없어 실제 DB 테스트를 건너뜁니다")
class PostChunkDbTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"DATABASE_URL": TEST_DATABASE_URL})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.cleanup)
        self.cleanup()
        self.store = EmbeddingStore()

    @staticmethod
    def cleanup():
        with psycopg.connect(TEST_DATABASE_URL) as conn, conn.cursor() as cur:
            cur.execute("delete from post_chunk_embedding where post_id like 'itest-chunk-%'")
            cur.execute("delete from post_embedding where post_id like 'itest-chunk-%'")

    def query(self, sql, params=()):
        with psycopg.connect(TEST_DATABASE_URL) as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def test_글_벡터와_조각을_함께_저장하고_상태를_읽는다(self):
        self.store.write(PID, "m", ("hp", vec(0)), [row(0, 0, 10, 1), row(1, 8, 20, 2)], "src1")

        self.assertEqual(self.store.get_state(PID), ("m", "hp"))
        self.assertEqual(self.store.get_chunk_state(PID), ("m", "src1", 2))
        rows = self.query("select idx, start_offset, end_offset, content_hash from post_chunk_embedding where post_id = %s order by idx", (PID,))
        self.assertEqual(rows, [(0, 0, 10, "h0"), (1, 8, 20, "h1")])

    def test_조각이_없으면_상태는_None(self):
        self.assertIsNone(self.store.get_chunk_state(PID))

    def test_다시_저장하면_그_글의_조각이_통째로_바뀐다(self):
        self.store.write(PID, "m", ("hp", vec(0)), [row(0, 0, 10, 1), row(1, 8, 20, 2), row(2, 18, 30, 3)], "src1")
        self.store.write(PID, "m", ("hp2", vec(0)), [row(0, 0, 12, 4)], "src2")

        self.assertEqual(self.store.get_chunk_state(PID), ("m", "src2", 1))  # 옛 idx 1, 2는 남지 않는다
        self.assertEqual(self.store.get_state(PID), ("m", "hp2"))

    def test_None은_건드리지_않고_빈_목록은_조각을_모두_지운다(self):
        self.store.write(PID, "m", ("hp", vec(0)), [row(0, 0, 10, 1)], "src1")

        self.store.write(PID, "m", ("hp2", vec(0)), None, "src1")  # 조각은 그대로
        self.assertEqual(self.store.get_chunk_state(PID), ("m", "src1", 1))

        self.store.write(PID, "m", None, [], "src1")  # 글 벡터는 그대로, 조각은 모두 삭제
        self.assertEqual(self.store.get_state(PID), ("m", "hp2"))
        self.assertIsNone(self.store.get_chunk_state(PID))

    def test_쓰는_도중_실패하면_글_벡터도_조각도_이전_상태로_남는다(self):
        self.store.write(PID, "m", ("hp", vec(0)), [row(0, 0, 10, 1), row(1, 8, 20, 2)], "src1")

        bad = ChunkRow(idx=1, start=30, end=30, digest="x", vector=vec(5))  # end > start 제약 위반
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.store.write(PID, "m2", ("hp2", vec(9)), [row(0, 0, 40, 4), bad], "src2")

        self.assertEqual(self.store.get_state(PID), ("m", "hp"))  # 글 벡터가 새 값으로 바뀌지 않았다
        self.assertEqual(self.store.get_chunk_state(PID), ("m", "src1", 2))  # 조각도 이전 그대로

    def test_모델이나_해시가_섞여_있으면_일치하지_않는_상태로_보고한다(self):
        self.store.write(PID, "m", None, [row(0, 0, 10, 1), row(1, 8, 20, 2)], "src1")
        with psycopg.connect(TEST_DATABASE_URL) as conn, conn.cursor() as cur:
            cur.execute("update post_chunk_embedding set source_hash = 'other' where post_id = %s and idx = 1", (PID,))

        self.assertEqual(self.store.get_chunk_state(PID), ("", "", 2))

    def test_삭제하면_글_벡터와_조각이_모두_지워진다(self):
        self.store.write(PID, "m", ("hp", vec(0)), [row(0, 0, 10, 1)], "src1")

        self.assertTrue(self.store.delete(PID))
        self.assertFalse(self.store.delete(PID))
        self.assertIsNone(self.store.get_state(PID))
        self.assertIsNone(self.store.get_chunk_state(PID))

    def test_조각만_남은_글도_삭제하면_True(self):
        self.store.write(PID, "m", None, [row(0, 0, 10, 1)], "src1")

        self.assertTrue(self.store.delete(PID))
        self.assertIsNone(self.store.get_chunk_state(PID))

    def test_같은_글의_같은_번호를_두_번_넣을_수_없다(self):
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.store.write(PID, "m", None, [row(0, 0, 10, 1), row(0, 8, 20, 2)], "src1")
        self.assertIsNone(self.store.get_chunk_state(PID))  # 실패했으니 하나도 남지 않는다


    # --- 조각 검색 ---
    def seed_search(self):
        # p1·p2·p3 모두 질문(vec(0))과 같은 방향의 조각 하나씩, p1은 직교 조각(vec(1))도 하나 더 가진다
        self.store.write("itest-chunk-p1", "m", ("h", vec(7)), [row(0, 0, 10, 0), row(1, 8, 20, 1)], "s1")
        self.store.write("itest-chunk-p2", "m", ("h", vec(7)), [row(0, 0, 10, 0)], "s2")
        self.store.write("itest-chunk-p3", "m", ("h", vec(7)), [row(0, 0, 10, 0)], "s3")

    def test_조각_검색은_후보_id_안의_글만_돌려준다(self):
        self.seed_search()

        found = self.store.similar_chunks_to_vector(vec(0), ["itest-chunk-p1", "itest-chunk-p3"], 10, 0.5)

        self.assertEqual(sorted(m.post_id for m in found), ["itest-chunk-p1", "itest-chunk-p3"])  # p2는 가까워도 후보 밖이라 없다

    def test_후보가_비어_있으면_아무것도_돌려주지_않는다(self):
        self.seed_search()

        self.assertEqual(self.store.similar_chunks_to_vector(vec(0), [], 10, 0.0), [])

    def test_기준_점수_미만_조각은_빠지고_점수순으로_나온다(self):
        self.seed_search()

        found = self.store.similar_chunks_to_vector(vec(0), ["itest-chunk-p1"], 10, 0.5)
        self.assertEqual([(m.idx, round(m.score, 3)) for m in found], [(0, 1.0)])  # 직교 조각(점수 0)은 기준 미만

        everything = self.store.similar_chunks_to_vector(vec(0), ["itest-chunk-p1"], 10, 0.0)
        self.assertEqual([m.idx for m in everything], [0, 1])  # 가까운 순

    def test_개수_상한을_지킨다(self):
        self.seed_search()

        found = self.store.similar_chunks_to_vector(vec(0), ["itest-chunk-p1", "itest-chunk-p2", "itest-chunk-p3"], 2, 0.5)

        self.assertEqual(len(found), 2)

    def test_조각_위치_조회는_요청한_글만_idx_순으로_준다(self):
        self.seed_search()

        layout = self.store.chunk_layout(["itest-chunk-p1", "itest-chunk-p2"])

        self.assertEqual(sorted(layout), ["itest-chunk-p1", "itest-chunk-p2"])  # p3은 요청하지 않았다
        self.assertEqual([(c.idx, c.start, c.end, c.digest) for c in layout["itest-chunk-p1"]], [(0, 0, 10, "h0"), (1, 8, 20, "h1")])
        self.assertEqual(self.store.chunk_layout([]), {})

    def test_조각이_없는_글만_글_벡터로_보충_검색한다(self):
        self.seed_search()
        self.store.write("itest-chunk-legacy", "m", ("h", vec(0)), None, "s")  # 글 벡터만 있고 조각은 없는 글
        ids = ["itest-chunk-p1", "itest-chunk-legacy"]

        with_chunks_too = self.store.similar_to_vector(vec(0), ids, 10, 0.5)
        only_legacy = self.store.similar_to_vector(vec(0), ids, 10, 0.5, without_chunks_only=True)

        self.assertEqual(sorted(m.post_id for m in with_chunks_too), ["itest-chunk-legacy"])  # p1의 글 벡터는 vec(7)이라 기준 미만
        self.assertEqual([m.post_id for m in only_legacy], ["itest-chunk-legacy"])
        near = self.store.similar_to_vector(vec(7), ids, 10, 0.5, without_chunks_only=True)
        self.assertEqual(near, [])  # p1은 글 벡터가 가까워도 조각이 있으니 보충 대상이 아니다


if __name__ == "__main__":
    unittest.main()
