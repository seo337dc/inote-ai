import unittest

from app.services.chunking import MAX_CHARS, OVERLAP_CHARS, build_chunk_embedding_text, chunk_text
from app.services.text_prep import html_to_text


def _paragraphs(count: int, size: int = 200) -> str:
    return "\n".join(f"문단{n} " + ("가" * (size - 6)) for n in range(count))


class ChunkTextTest(unittest.TestCase):
    def test_빈_텍스트는_청크가_없다(self):
        self.assertEqual(chunk_text(""), [])
        self.assertEqual(chunk_text("  \n \n"), [])
        self.assertEqual(chunk_text(None), [])  # type: ignore[arg-type]

    def test_짧은_글은_청크_하나로_글_전체다(self):
        text = "첫 문단\n둘째 문단"
        chunks = chunk_text(text)
        self.assertEqual(len(chunks), 1)
        self.assertEqual((chunks[0].start, chunks[0].end, chunks[0].text), (0, len(text), text))

    def test_청크는_원문의_연속된_조각이다_start_end로_그대로_잘라_낼_수_있다(self):
        text = _paragraphs(20)
        for c in chunk_text(text):
            self.assertEqual(c.text, text[c.start : c.end])

    def test_모든_청크는_최대_길이_이하다(self):
        for text in (_paragraphs(30), "가" * 5000, "문장입니다. " * 600):
            for c in chunk_text(text):
                self.assertLessEqual(len(c.text), MAX_CHARS)

    def test_글_전체가_빠짐없이_청크에_들어간다(self):
        text = _paragraphs(25) + "\n마지막 문단입니다."
        covered = set()
        for c in chunk_text(text):
            covered.update(range(c.start, c.end))
        missing = [i for i, ch in enumerate(text) if not ch.isspace() and i not in covered]
        self.assertEqual(missing, [])

    def test_순서대로_앞으로_나아간다(self):
        chunks = chunk_text(_paragraphs(30))
        self.assertEqual([c.index for c in chunks], list(range(len(chunks))))
        for a, b in zip(chunks, chunks[1:]):
            self.assertGreater(b.start, a.start)
            self.assertGreater(b.end, a.end)

    def test_이웃한_청크는_겹침_범위_안에서_끝_문단을_공유한다(self):
        text = "\n".join(f"문단{n:02d} " + ("나" * 50) for n in range(40))  # 문단 하나가 약 57자
        chunks = chunk_text(text)
        self.assertGreater(len(chunks), 2)
        for a, b in zip(chunks, chunks[1:]):
            overlap = a.end - b.start
            self.assertGreater(overlap, 0)
            self.assertLessEqual(overlap, OVERLAP_CHARS)

    def test_문단이_겹침보다_길면_겹치지_않고_문단_경계에서_끊는다(self):
        chunks = chunk_text(_paragraphs(12, size=300))
        for a, b in zip(chunks, chunks[1:]):
            self.assertGreaterEqual(b.start, a.end)  # 겹치지 않는다
        self.assertTrue(all(c.text.startswith("문단") for c in chunks))

    def test_문단은_가능하면_쪼개지_않는다(self):
        text = _paragraphs(12, size=300)
        starts = {m for m in range(len(text)) if m == 0 or text[m - 1] == "\n"}
        for c in chunk_text(text):
            self.assertIn(c.start, starts)

    def test_긴_문단은_문장_경계에서_나눈다(self):
        sentence = "이것은 한 문장입니다."
        text = " ".join([sentence] * 200)  # 줄바꿈 없는 긴 문단
        chunks = chunk_text(text)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertTrue(c.text.endswith("."), c.text[-10:])
            self.assertTrue(c.text.startswith("이것은"))

    def test_공백도_문장_부호도_없는_긴_줄도_끝나고_모두_들어간다(self):
        text = "가" * 4321
        chunks = chunk_text(text)
        self.assertEqual(sum(len(c.text) for c in chunks) >= len(text), True)
        covered = set()
        for c in chunks:
            covered.update(range(c.start, c.end))
        self.assertEqual(len(covered), len(text))

    def test_끝에_너무_짧은_조각이_남으면_앞_청크에_붙인다(self):
        text = _paragraphs(4, size=200) + "\n끝"
        chunks = chunk_text(text, target_chars=600, max_chars=1000)
        self.assertEqual(chunks[-1].end, len(text))
        self.assertGreater(len(chunks[-1].text), 10)  # "끝"만 있는 청크가 아니다

    def test_같은_입력은_항상_같은_결과다(self):
        text = _paragraphs(30)
        self.assertEqual(chunk_text(text), chunk_text(text))

    def test_HTML에서_만든_텍스트도_그대로_쓸_수_있다(self):
        html = "".join(f"<p>문단 {n} " + "본문 " * 60 + "</p>" for n in range(15))
        text = html_to_text(html)
        chunks = chunk_text(text)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertEqual(c.text, text[c.start : c.end])


class BuildChunkEmbeddingTextTest(unittest.TestCase):
    def test_제목_줄바꿈_청크_순서로_붙인다(self):
        self.assertEqual(build_chunk_embedding_text(" 제목 ", " 조각 "), "제목\n조각")


if __name__ == "__main__":
    unittest.main()
