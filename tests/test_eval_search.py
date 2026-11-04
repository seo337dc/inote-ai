"""검색 평가 스크립트 테스트 — 가짜 저장소·가짜 임베딩 서비스로 돈다 (DB·네트워크 없음)."""

import json
import os
import tempfile
import unittest
from unittest import mock

from app.services.embedder_client import EmbedResult
from app.services.embedding_store import ChunkMatch, Match
from scripts import eval_search
from scripts.eval_search import (
    Question,
    QuestionFileError,
    evaluate,
    format_report,
    load_questions,
    mrr,
    parse_thresholds,
    rank_of,
    recall_at,
    sweep,
)

DIM = 1024


class FakeEmbedder:
    """질문 번호(= 질문 목록의 순서)를 벡터 첫 칸에 실어 보낸다 — 가짜 저장소가 그 번호로 점수표를 찾는다."""

    def __init__(self, texts):
        self.index = {t: i for i, t in enumerate(texts)}

    def embed(self, inputs):
        return EmbedResult(model="m", dim=DIM, embeddings=[[float(self.index[t])] + [0.0] * (DIM - 1) for t in inputs])


class FakeStore:
    def __init__(self, chunk_scores, post_scores=None):
        # chunk_scores[i] = [(post_id, idx, score), ...] — i번째 질문에 대한 조각 점수표
        self.chunk_scores, self.post_scores = chunk_scores, post_scores or {}

    def similar_chunks_to_vector(self, vector, candidate_ids, limit, min_score):
        rows = [ChunkMatch(p, i, s) for p, i, s in self.chunk_scores[int(vector[0])] if p in candidate_ids and s >= min_score]
        return sorted(rows, key=lambda m: -m.score)[:limit]

    def similar_to_vector(self, vector, candidate_ids, limit, min_score, without_chunks_only=False):
        if without_chunks_only:
            return []
        rows = [Match(p, s) for p, s in self.post_scores.get(int(vector[0]), []) if p in candidate_ids and s >= min_score]
        return sorted(rows, key=lambda m: -m.score)[:limit]


class MetricsTest(unittest.TestCase):
    def test_정답_중_가장_높은_순위를_돌려주고_없으면_None(self):
        self.assertEqual(rank_of(["a", "b", "c"], ["c", "b"]), 2)
        self.assertEqual(rank_of(["a", "b"], ["x"]), None)
        self.assertEqual(rank_of([], ["x"]), None)

    def test_Recall과_MRR(self):
        ranks = [1, 3, None, 2]

        self.assertEqual(recall_at(ranks, 1), 0.25)
        self.assertEqual(recall_at(ranks, 3), 0.75)
        self.assertAlmostEqual(mrr(ranks), (1 + 1 / 3 + 0 + 1 / 2) / 4)
        self.assertEqual((recall_at([], 3), mrr([])), (0.0, 0.0))


class LoadQuestionsTest(unittest.TestCase):
    def write(self, data):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        self.addCleanup(os.unlink, f.name)
        json.dump(data, f, ensure_ascii=False)
        f.close()
        return f.name

    def test_정답이_있는_질문과_없어야_하는_질문을_읽는다(self):
        qs = load_questions(self.write([{"question": " 질문 ", "expect": ["a", "a", "b"]}, {"question": "무관", "expect": []}, {"question": "생략"}]))

        self.assertEqual(qs, [Question("질문", ["a", "b"]), Question("무관", []), Question("생략", [])])
        self.assertEqual([q.is_negative for q in qs], [False, True, True])

    def test_형식이_틀리면_어느_항목인지_알려_준다(self):
        for bad in ([], {}, [{"expect": []}], [{"question": " "}], [{"question": "q", "expect": "a"}], [{"question": "q", "expect": [1]}], ["q"]):
            with self.assertRaises(QuestionFileError, msg=str(bad)):
                load_questions(self.write(bad))

    def test_없는_파일이나_JSON이_아닌_파일(self):
        with self.assertRaises(QuestionFileError):
            load_questions("/nonexistent/questions.json")
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        self.addCleanup(os.unlink, f.name)
        f.write("not json")
        f.close()
        with self.assertRaises(QuestionFileError):
            load_questions(f.name)


class EvaluateAndSweepTest(unittest.TestCase):
    IDS = ["p1", "p2", "p3"]

    def setUp(self):
        self.questions = [
            Question("정답이 p2인 질문", ["p2"]),  # 0번
            Question("정답이 p3인데 점수가 낮은 질문", ["p3"]),  # 1번
            Question("관련 없는 질문", []),  # 2번
        ]
        self.store = FakeStore(
            chunk_scores={
                0: [("p1", 0, 0.60), ("p2", 3, 0.80), ("p2", 1, 0.70)],  # p2의 가장 가까운 조각이 0.80
                1: [("p1", 0, 0.62), ("p3", 5, 0.55)],  # p3가 2등, 점수도 낮다
                2: [("p1", 0, 0.52), ("p2", 0, 0.45)],  # 관련 없는데 0.52가 나온다
            },
            post_scores={0: [("p1", 0.60), ("p2", 0.50)], 1: [("p1", 0.5), ("p3", 0.4)], 2: [("p1", 0.4)]},
        )
        self.embedder = FakeEmbedder([q.text for q in self.questions])
        self.results = evaluate(self.questions, self.store, self.embedder, self.IDS)

    def test_글_점수는_가장_가까운_조각_점수이고_글_순위는_점수순(self):
        r = self.results[0]

        self.assertEqual(r.chunk_ranking, [("p2", 0.80), ("p1", 0.60)])
        self.assertEqual((r.rank("chunk"), r.rank("post")), (1, 2))  # 조각 검색은 1등, 글 벡터는 2등
        self.assertEqual(r.top_score("chunk"), 0.80)

    def test_순위와_1등_점수는_질문마다_따로_계산된다(self):
        self.assertEqual([r.rank("chunk") for r in self.results], [1, 2, None])
        self.assertEqual(self.results[2].top_score("chunk"), 0.52)

    def test_기준_점수가_낮으면_오탐이_생기고_높으면_정답을_놓친다(self):
        rows = {row.threshold: row for row in sweep(self.results, self.store, self.IDS, [0.40, 0.50, 0.70, 0.90], margin=0.0)}

        self.assertEqual(rows[0.40].false_positive, 1.0)  # 관련 없는 질문에도 글을 보여준다
        self.assertEqual(rows[0.50].false_positive, 1.0)  # 최대 점수 0.52가 0.50을 넘는다
        self.assertEqual(rows[0.70].false_positive, 0.0)
        self.assertEqual(rows[0.70].shown_correct, 0.5)  # p2 질문은 보여주고, 점수가 낮은 p3 질문은 놓친다
        self.assertEqual(rows[0.70].shown_nothing, 0.5)
        self.assertEqual(rows[0.90].shown_correct, 0.0)

    def test_margin은_1등과_가까운_글을_함께_보여주므로_정답_외_글이_늘어난다(self):
        strict = sweep(self.results, self.store, self.IDS, [0.60], margin=0.0)[0]
        loose = sweep(self.results, self.store, self.IDS, [0.60], margin=0.2)[0]

        self.assertGreaterEqual(loose.avg_extra, strict.avg_extra)

    def test_정답이_여러_편이면_포함률은_보여준_정답_수로_계산한다(self):
        # 4번 질문: 정답 p1·p2 모두 상위에 있다(점수 0.80, 0.78). 5번 질문: 정답 p1·p3 중 p1만 상위(p3은 0.30)
        store = FakeStore(chunk_scores={0: [("p1", 0, 0.80), ("p2", 0, 0.78)], 1: [("p1", 0, 0.80), ("p3", 0, 0.30)]})
        questions = [Question("둘 다 정답", ["p1", "p2"]), Question("하나만 정답", ["p1", "p3"])]
        results = evaluate(questions, store, FakeEmbedder([q.text for q in questions]), self.IDS)

        row = sweep(results, store, self.IDS, [0.50], margin=0.0)[0]

        self.assertEqual(row.coverage, (2 / 2 + 1 / 2) / 2)  # 첫 질문은 둘 다, 둘째는 둘 중 하나
        self.assertEqual(row.shown_correct, 1.0)  # "하나라도 맞으면"으로 보면 둘 다 적중

    def test_정답이_limit보다_많으면_limit편_기준으로_센다(self):
        store = FakeStore(chunk_scores={0: [(p, 0, 0.8) for p in ("p1", "p2", "p3")]})
        questions = [Question("정답이 네 편", ["p1", "p2", "p3", "p4"])]
        results = evaluate(questions, store, FakeEmbedder([q.text for q in questions]), ["p1", "p2", "p3", "p4"])

        row = sweep(results, store, ["p1", "p2", "p3", "p4"], [0.50], margin=0.0, limit=3)[0]

        self.assertEqual(row.coverage, 1.0)  # 3편을 다 보여줬으면 최대치(3/3)

    def test_리포트에_핵심_수치가_들어_있다(self):
        rows = sweep(self.results, self.store, self.IDS, [0.50, 0.70], margin=0.09)

        text = format_report(self.results, rows, 0.09, len(self.IDS))

        for expected in ("질문 3개", "정답 있음 2", "관련 글 없어야 함 1", "Recall@1", "MRR", "관련 없는 질문", "기준 점수 스윕", "정답 포함률", "0.52"):
            self.assertIn(expected, text)

    def test_정답_또는_관련없는_질문이_없으면_경고한다(self):
        only_pos = evaluate(self.questions[:2], self.store, self.embedder, self.IDS)

        text = format_report(only_pos, sweep(only_pos, self.store, self.IDS, [0.5], 0.0), 0.0, 3)

        self.assertIn("모두 있어야", text)


class CliTest(unittest.TestCase):
    def run_main(self, argv, env):
        with mock.patch.dict(os.environ, env, clear=True):
            return eval_search.main(argv)

    def test_DATABASE_URL이_없으면_실행하지_않는다(self):
        self.assertEqual(self.run_main([], {}), 2)

    def test_개발_DB가_아니면_거절한다_DB에_연결하기_전에(self):
        with mock.patch.object(eval_search, "fetch_ids", side_effect=AssertionError("연결하면 안 된다")):
            code = self.run_main(["--questions", "x.json"], {"DATABASE_URL": "postgresql://u:p@aws-0.pooler.supabase.com:5432/postgres"})

        self.assertEqual(code, 2)

    def test_질문_파일이_없으면_2(self):
        env = {"DATABASE_URL": "postgresql://u:p@127.0.0.1:5434/inote_ai_dev"}

        self.assertEqual(self.run_main(["--questions", "/nonexistent.json"], env), 2)

    def test_기준_점수_후보는_0에서_1_사이_숫자여야_한다(self):
        self.assertEqual(parse_thresholds("0.4, 0.5"), [0.4, 0.5])
        for bad in ("", "abc", "0.5,1.5", "-0.1"):
            with self.assertRaises(ValueError, msg=bad):
                parse_thresholds(bad)


if __name__ == "__main__":
    unittest.main()
