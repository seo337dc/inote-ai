import unittest

from app.services.chunk_excerpt import build_excerpt
from app.services.chunking import build_chunk_embedding_text, chunk_text
from app.services.embedding_store import ChunkInfo
from app.services.text_prep import content_hash

TITLE = "도커 정리"


def long_text(paragraphs=40):
    return "\n".join(f"문단{n:02d} " + ("가나다라" * 60) for n in range(paragraphs))


def layout_of(plain, title=TITLE):
    """저장 때와 같은 방식으로 만든 조각 위치·해시 목록."""
    return [
        ChunkInfo(c.index, c.start, c.end, content_hash(build_chunk_embedding_text(title, c.text)))
        for c in chunk_text(plain)
    ]


class BuildExcerptTest(unittest.TestCase):
    def setUp(self):
        self.plain = long_text()
        self.layout = layout_of(self.plain)
        self.assertGreater(len(self.layout), 5)

    def test_찾은_조각과_앞뒤_이웃_조각의_원문을_돌려준다(self):
        excerpt = build_excerpt(self.plain, TITLE, self.layout, [3])

        self.assertIsNotNone(excerpt)
        c = {i.idx: i for i in self.layout}
        self.assertTrue(excerpt.startswith(self.plain[c[2].start : c[2].start + 20]))  # 앞 조각부터
        self.assertTrue(excerpt.endswith(self.plain[c[4].end - 20 : c[4].end]))  # 뒤 조각까지
        self.assertNotIn(self.plain[c[0].start : c[0].start + 6], excerpt)  # 이웃이 아닌 조각은 들어가지 않는다

    def test_겹치는_부분은_한_번만_들어간다(self):
        excerpt = build_excerpt(self.plain, TITLE, self.layout, [3])

        c = {i.idx: i for i in self.layout}
        self.assertEqual(excerpt, self.plain[c[2].start : c[4].end])  # 세 조각이 이어진 구간 하나(겹침 중복 없음)

    def test_떨어진_조각은_생략_표시로_잇는다(self):
        excerpt = build_excerpt(self.plain, TITLE, self.layout, [1, len(self.layout) - 2], neighbors=0)

        self.assertIn("\n…\n", excerpt)

    def test_이웃_조각이_없는_가장자리도_된다(self):
        first = build_excerpt(self.plain, TITLE, self.layout, [0])
        last = build_excerpt(self.plain, TITLE, self.layout, [len(self.layout) - 1])

        self.assertTrue(first.startswith(self.plain[:10]))
        self.assertTrue(last.endswith(self.plain[-10:]))

    def test_글자_수_상한을_넘으면_이웃_조각부터_버린다(self):
        excerpt = build_excerpt(self.plain, TITLE, self.layout, [3], max_chars=1000)

        c = {i.idx: i for i in self.layout}
        self.assertLessEqual(len(excerpt), 1000)
        self.assertIn(self.plain[c[3].start : c[3].start + 30], excerpt)  # 찾은 조각은 반드시 남는다

    def test_원문이_바뀌어_해시가_다르면_None(self):
        changed = self.plain.replace("문단04", "수정됨", 1)

        self.assertIsNone(build_excerpt(changed, TITLE, self.layout, [4]))

    def test_글이_바뀌지_않았어도_제목이_다르면_None(self):
        self.assertIsNone(build_excerpt(self.plain, "다른 제목", self.layout, [3]))

    def test_이웃_조각만_바뀌었어도_None(self):
        c = {i.idx: i for i in self.layout}
        mid = (c[4].start + c[4].end) // 2  # 4번 조각의 한가운데 — 3번(찾은 조각)과 겹치는 구간이 아니다
        changed = self.plain[:mid] + "XXXXXX" + self.plain[mid + 6 :]

        self.assertIsNone(build_excerpt(changed, TITLE, self.layout, [3]))  # 이웃 4번의 글자가 달라졌다

    def test_원문이_짧아져_위치가_범위를_벗어나면_None(self):
        self.assertIsNone(build_excerpt(self.plain[:500], TITLE, self.layout, [5]))

    def test_쓸_조각이_없으면_None(self):
        self.assertIsNone(build_excerpt(self.plain, TITLE, self.layout, []))
        self.assertIsNone(build_excerpt(self.plain, TITLE, [], [3]))
        self.assertIsNone(build_excerpt(self.plain, TITLE, self.layout, [999]))


if __name__ == "__main__":
    unittest.main()
