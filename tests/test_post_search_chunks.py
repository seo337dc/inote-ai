"""PostSearcher의 조각 검색 — 가짜 BE·가짜 임베딩 서비스·가짜 저장소로 돈다 (DB·네트워크 없음).
조각 SQL(후보 id 제한 등)은 tests/test_post_chunk_db.py(실제 DB)가 확인한다."""

import asyncio
import unittest

from app.services.chunking import build_chunk_embedding_text, chunk_text
from app.services.embedding_store import ChunkInfo, ChunkMatch, Match
from app.services.post_search import MAX_CHARS_PER_POST, PostHit, PostSearcher, group_hits
from app.services.text_prep import content_hash, html_to_text
from tests.test_posts_chat import FakeEmbedder, FakeServer, FakeStore, post

TITLE = "Docker - Volume"


def html_of(paragraphs=40):
    return "".join(f"<p>문단{n:02d} " + ("가나다라" * 60) + "</p>" for n in range(paragraphs))


def layout_for(html, title=TITLE):
    plain = html_to_text(html)
    return [ChunkInfo(c.index, c.start, c.end, content_hash(build_chunk_embedding_text(title, c.text))) for c in chunk_text(plain)]


class GroupHitsTest(unittest.TestCase):
    def test_글_점수는_가장_가까운_조각_점수이고_조각은_가까운_순이다(self):
        hits = group_hits(
            [ChunkMatch("a", 2, 0.6), ChunkMatch("a", 5, 0.8), ChunkMatch("b", 0, 0.7)], [], limit=5
        )

        self.assertEqual(hits, [PostHit("a", 0.8, [5, 2]), PostHit("b", 0.7, [0])])

    def test_한_글에_조각이_몰려도_다른_글이_밀려나지_않는다(self):
        chunks = [ChunkMatch("a", i, 0.9 - i * 0.01) for i in range(6)] + [ChunkMatch("b", 0, 0.5)]

        self.assertEqual([h.post_id for h in group_hits(chunks, [], limit=2)], ["a", "b"])

    def test_조각이_없는_글은_글_벡터_결과로_보충하고_점수순에_섞는다(self):
        hits = group_hits([ChunkMatch("a", 0, 0.6)], [Match("legacy", 0.7)], limit=5)

        self.assertEqual([(h.post_id, h.chunks) for h in hits], [("legacy", []), ("a", [0])])

    def test_조각이_있는_글은_글_벡터_결과가_와도_조각_결과를_쓴다(self):
        hits = group_hits([ChunkMatch("a", 3, 0.6)], [Match("a", 0.95)], limit=5)

        self.assertEqual(hits, [PostHit("a", 0.6, [3])])

    def test_상위_limit개만_남긴다(self):
        hits = group_hits([ChunkMatch(str(i), 0, 0.5 + i / 100) for i in range(10)], [], limit=3)

        self.assertEqual(len(hits), 3)
        self.assertEqual(hits[0].post_id, "9")


class ChunkSearchTest(unittest.TestCase):
    def setUp(self):
        self.server, self.embedder, self.store = FakeServer(), FakeEmbedder(), FakeStore()
        self.server.ids = ["a"]
        self.html = html_of()
        self.server.posts["a"] = post("a", title=TITLE, content=self.html)
        self.store.layout["a"] = layout_for(self.html)
        self.assertGreater(len(self.store.layout["a"]), 10)
        plain = html_to_text(self.html)
        self.marker = plain[self.store.layout["a"][9].start : self.store.layout["a"][9].start + 6]  # 9번 조각 안의 문단 표지

    def search(self):
        return asyncio.run(PostSearcher(self.server, self.embedder, self.store, min_score=0.5, score_margin=0.0).search("질문"))

    def test_뒷부분_조각에_걸린_글은_앞부분이_아니라_그_조각의_원문을_근거로_준다(self):
        self.store.chunk_matches = [ChunkMatch("a", 9, 0.7)]

        outcome = self.search()

        self.assertEqual(outcome.status, "found")
        text = outcome.posts[0].text
        self.assertIn(self.marker, text)
        self.assertNotIn("문단00", text)  # 예전 방식(앞 1,500자)이라면 여기가 근거였다
        self.assertEqual(outcome.posts[0].score, 0.7)

    def test_조각_검색은_후보_id만_넘긴다(self):
        self.server.ids = ["a", "pub2"]
        self.store.chunk_matches = [ChunkMatch("a", 1, 0.7)]

        self.search()

        self.assertEqual(self.store.chunk_calls[0][0], ["a", "pub2"])

    def test_글이_바뀌어_해시가_다르면_앞부분_텍스트로_대신한다(self):
        self.store.chunk_matches = [ChunkMatch("a", 9, 0.7)]
        self.server.posts["a"] = post("a", title=TITLE, content=self.html.replace(self.marker, "수정됨"))

        outcome = self.search()

        self.assertEqual(outcome.status, "found")
        self.assertIn("문단00", outcome.posts[0].text)
        self.assertLessEqual(len(outcome.posts[0].text), MAX_CHARS_PER_POST)

    def test_조각_위치를_읽지_못해도_앞부분_텍스트로_검색은_된다(self):
        self.store.chunk_matches = [ChunkMatch("a", 9, 0.7)]
        self.store.layout_down = True

        outcome = self.search()

        self.assertEqual(outcome.status, "found")
        self.assertIn("문단00", outcome.posts[0].text)

    def test_조각이_없는_글은_글_벡터로_찾고_앞부분을_근거로_준다(self):
        self.store.chunk_matches = []
        self.store.matches = [Match("a", 0.7)]

        outcome = self.search()

        self.assertEqual(outcome.status, "found")
        self.assertIn("문단00", outcome.posts[0].text)

    def test_기준_점수를_못_넘으면_관련_글_없음(self):
        self.store.chunk_matches = [ChunkMatch("a", 9, 0.3)]

        self.assertEqual(self.search().status, "none")

    def test_근거_텍스트는_글_하나당_상한을_넘지_않는다(self):
        self.store.chunk_matches = [ChunkMatch("a", i, 0.7) for i in range(10)]

        from app.services.chunk_excerpt import MAX_EXCERPT_CHARS

        self.assertLessEqual(len(self.search().posts[0].text), MAX_EXCERPT_CHARS + 10)


if __name__ == "__main__":
    unittest.main()
