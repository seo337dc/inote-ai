"""EmbeddingStore를 실제 pgvector DB로 확인하는 통합 테스트 — 특히 "후보 id 밖의 글은 절대 나오지 않는다".

TEST_DATABASE_URL이 있을 때만 돈다 (없으면 건너뜀). 로컬 개발 DB를 쓴다:
  TEST_DATABASE_URL="postgresql://ai:<AI_DEV_DB_PASSWORD>@127.0.0.1:5434/inote_ai_dev" python -m unittest tests.test_embedding_store_db -v
테스트가 만든 행(post_id가 'itest-'로 시작)은 끝나면 지운다. 운영 DB로 돌리지 않는다.
"""

import math
import os
import unittest
from typing import Optional
from unittest import mock

import psycopg

from app.services.embedding_store import EmbeddingStore

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
DIM = 1024


def vec(i: int, j: Optional[int] = None, wj: float = 0.0, wi: float = 1.0):
    """i번째 칸 wi, j번째 칸 wj, 나머지 0인 정규화된 1024차원 벡터."""
    v = [0.0] * DIM
    v[i] = wi
    if j is not None:
        v[j] = wj
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]


@unittest.skipUnless(TEST_DATABASE_URL, "TEST_DATABASE_URL이 없어 실제 DB 테스트를 건너뜁니다")
class EmbeddingStoreDbTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"DATABASE_URL": TEST_DATABASE_URL})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.cleanup)
        self.cleanup()
        self.store = EmbeddingStore()
        # 기준 글 SRC = e0. NEAR는 거의 같은 방향, MID는 45도, FAR는 직교, OPP는 반대 방향
        self.store.upsert("itest-src", "m", "h-src", vec(0))
        self.store.upsert("itest-near", "m", "h-near", vec(0, 1, 0.1, wi=0.9))
        self.store.upsert("itest-mid", "m", "h-mid", vec(0, 2, 1.0))
        self.store.upsert("itest-far", "m", "h-far", vec(3))
        self.store.upsert("itest-opp", "m", "h-opp", vec(0, wi=-1.0))

    @staticmethod
    def cleanup():
        with psycopg.connect(TEST_DATABASE_URL) as conn, conn.cursor() as cur:
            cur.execute("delete from post_embedding where post_id like 'itest-%'")

    ALL = ["itest-src", "itest-near", "itest-mid", "itest-far", "itest-opp"]

    def ids(self, matches):
        return [m.post_id for m in matches]

    # --- 저장 ---
    def test_저장한_상태를_읽고_다시_저장하면_덮어쓴다(self):
        self.assertEqual(self.store.get_state("itest-src"), ("m", "h-src"))
        self.store.upsert("itest-src", "m2", "h-src2", vec(5))

        self.assertEqual(self.store.get_state("itest-src"), ("m2", "h-src2"))
        with psycopg.connect(TEST_DATABASE_URL) as conn, conn.cursor() as cur:
            cur.execute("select count(*) from post_embedding where post_id = 'itest-src'")
            self.assertEqual(cur.fetchone()[0], 1)

    def test_없는_글의_상태는_None(self):
        self.assertIsNone(self.store.get_state("itest-nothing"))

    def test_삭제는_지웠으면_True_없으면_False(self):
        self.assertTrue(self.store.delete("itest-far"))
        self.assertFalse(self.store.delete("itest-far"))
        self.assertIsNone(self.store.get_state("itest-far"))

    # --- 저장된 글 기준 유사 글 ---
    def test_가까운_순서로_돌려주고_자기_자신은_뺀다(self):
        matches = self.store.similar_to_post("itest-src", self.ALL, limit=10, min_score=-1.0)

        self.assertEqual(self.ids(matches), ["itest-near", "itest-mid", "itest-far", "itest-opp"])
        scores = [m.score for m in matches]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertAlmostEqual(matches[0].score, 0.9 / math.sqrt(0.9**2 + 0.1**2), places=5)  # 코사인 유사도
        self.assertAlmostEqual(matches[2].score, 0.0, places=5)  # 직교
        self.assertAlmostEqual(matches[3].score, -1.0, places=5)  # 반대 방향

    def test_후보_밖의_글은_가장_가까워도_나오지_않는다(self):
        matches = self.store.similar_to_post("itest-src", ["itest-mid", "itest-far"], limit=10, min_score=-1.0)

        self.assertEqual(self.ids(matches), ["itest-mid", "itest-far"])  # itest-near(가장 가까움)와 itest-opp는 후보가 아니라서 없다

    def test_후보가_비어_있으면_아무것도_나오지_않는다(self):
        self.assertEqual(self.store.similar_to_post("itest-src", [], limit=10, min_score=-1.0), [])

    def test_후보에_있어도_벡터가_없는_글이나_모르는_id는_조용히_빠진다(self):
        matches = self.store.similar_to_post("itest-src", ["itest-mid", "itest-not-embedded", "other-db-id"], limit=10, min_score=-1.0)

        self.assertEqual(self.ids(matches), ["itest-mid"])

    def test_limit과_minScore를_지킨다(self):
        self.assertEqual(self.ids(self.store.similar_to_post("itest-src", self.ALL, limit=2, min_score=-1.0)), ["itest-near", "itest-mid"])
        self.assertEqual(self.ids(self.store.similar_to_post("itest-src", self.ALL, limit=10, min_score=0.5)), ["itest-near", "itest-mid"])
        self.assertEqual(self.store.similar_to_post("itest-src", self.ALL, limit=10, min_score=0.999), [])  # NEAR의 유사도는 약 0.9939

    def test_기준_글의_벡터가_없으면_None(self):
        self.assertIsNone(self.store.similar_to_post("itest-not-embedded", self.ALL, limit=5, min_score=0.0))

    def test_기준_글은_있지만_후보가_모두_빠지면_None이_아니라_빈_목록(self):
        self.assertEqual(self.store.similar_to_post("itest-src", ["itest-src"], limit=5, min_score=0.0), [])

    # --- 저장 전 텍스트(벡터) 기준 유사 글 ---
    def test_벡터_기준도_후보_안에서만_가까운_순서로(self):
        matches = self.store.similar_to_vector(vec(0), ["itest-mid", "itest-far", "itest-opp"], limit=10, min_score=-1.0)

        self.assertEqual(self.ids(matches), ["itest-mid", "itest-far", "itest-opp"])  # itest-src·itest-near는 후보가 아니라서 없다
        self.assertAlmostEqual(matches[0].score, math.sqrt(0.5), places=5)

    def test_벡터_기준에서_빈_후보_minScore_limit(self):
        self.assertEqual(self.store.similar_to_vector(vec(0), [], limit=5, min_score=-1.0), [])
        self.assertEqual(self.ids(self.store.similar_to_vector(vec(0), self.ALL, limit=2, min_score=-1.0)), ["itest-src", "itest-near"])
        self.assertEqual(self.ids(self.store.similar_to_vector(vec(0), self.ALL, limit=10, min_score=0.6)), ["itest-src", "itest-near", "itest-mid"])

    # --- search_path에 extensions가 없어도 동작한다 (운영 Supabase의 search_path를 아직 확인하기 전이라) ---
    def test_search_path에_extensions가_없는_접속에서도_동작한다(self):
        sep = "&" if "?" in TEST_DATABASE_URL else "?"
        url = f"{TEST_DATABASE_URL}{sep}options=-c%20search_path%3Dpublic"
        with mock.patch.dict(os.environ, {"DATABASE_URL": url}):
            with psycopg.connect(url) as conn, conn.cursor() as cur:
                cur.execute("show search_path")
                self.assertNotIn("extensions", cur.fetchone()[0])  # 조건이 실제로 걸렸는지 확인

            store = EmbeddingStore()
            self.assertEqual(self.ids(store.similar_to_post("itest-src", self.ALL, limit=2, min_score=-1.0)), ["itest-near", "itest-mid"])
            self.assertEqual(self.ids(store.similar_to_vector(vec(0), self.ALL, limit=2, min_score=-1.0)), ["itest-src", "itest-near"])
            store.upsert("itest-extra", "m", "h", vec(7))  # 저장(::extensions.vector 캐스트)도 동작
            self.assertEqual(store.get_state("itest-extra"), ("m", "h"))


if __name__ == "__main__":
    unittest.main()
