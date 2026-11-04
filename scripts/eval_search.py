"""글 찾기 검색 평가 — 질문 세트로 "정답 글을 몇 등에 찾는지"와 "관련 없는 질문에 얼마나 엉뚱한 글을 보여주는지"를 잰다.
(Vercel 번들에는 들어가지 않는다: .vercelignore)

  # 개발 DB + 로컬 임베딩 서비스 (레포 루트에서)
  DATABASE_URL="postgresql://ai:<AI_DEV_DB_PASSWORD>@127.0.0.1:5434/inote_ai_dev" \\
  EMBEDDER_URL="http://127.0.0.1:8100" EMBEDDER_SECRET="<임베딩 서비스 INTERNAL_SECRET>" \\
  venv/bin/python -m scripts.eval_search [--questions eval/questions.json]

질문 파일(JSON 배열): {"question": "...", "expect": ["정답 글 id", ...]}  — expect가 빈 배열이면 "관련 글이 없어야 하는 질문"이다. 형식 예: eval/questions.example.json

무엇을 재나
1. 정답이 있는 질문: 정답 글의 순위(조각 검색 / 예전 글 벡터 검색), Recall@1·@3, MRR
2. 관련 없는 질문: 가장 높은 점수(= 기준 점수를 이보다 높게 잡아야 오탐이 없다)
3. 기준 점수(POSTS_CHAT_MIN_SCORE) 스윕: 후보 값마다 "정답을 보여준 비율", "정답 글을 빠짐없이 보여준 정도(포함률)", "관련 없는 질문에도 글을 보여준 비율" — 실제 글 찾기와 같은 선택 규칙(group_hits·_select)을 그대로 쓴다

안전장치: .env를 읽지 않고, 개발 DB(로컬 호스트 + 이름이 _dev로 끝남)가 아니면 --allow-remote 없이는 실행하지 않는다. 읽기만 하며 DB에 쓰지 않는다.
종료 코드: 0 평가 완료 / 2 실행하지 못함(설정 오류·질문 파일 오류·대상 거절)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from app.db.postgres import get_connection
from app.services.embedder_client import EmbedderClient, EmbedderUnavailable, configured_retries, configured_timeout
from app.services.embedding_store import EmbeddingStore
from app.services.post_search import CHUNK_CANDIDATES, DEFAULT_LIMIT, PostSearcher, get_score_margin, group_hits
from scripts.embedding_worker import TargetNotAllowed, check_target

DEFAULT_THRESHOLDS = [0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80]
RANKING_DEPTH = 300  # 순위를 잴 때 가져오는 조각·글 수(전부 가져와도 규모가 작다)


class QuestionFileError(Exception):
    """질문 파일 형식이 잘못됐다."""


@dataclass(frozen=True)
class Question:
    text: str
    expect: List[str]

    @property
    def is_negative(self) -> bool:
        return not self.expect


def load_questions(path: str) -> List[Question]:
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except OSError as e:
        raise QuestionFileError(f"질문 파일을 읽을 수 없습니다: {path}") from e
    except ValueError as e:
        raise QuestionFileError(f"질문 파일이 올바른 JSON이 아닙니다: {path}") from e
    if not isinstance(raw, list) or not raw:
        raise QuestionFileError("질문 파일은 비어 있지 않은 JSON 배열이어야 합니다.")
    questions: List[Question] = []
    for i, item in enumerate(raw, 1):
        if not isinstance(item, dict) or not isinstance(item.get("question"), str) or not item["question"].strip():
            raise QuestionFileError(f"{i}번째 항목: 'question'은 비어 있지 않은 문자열이어야 합니다.")
        expect = item.get("expect", [])
        if not isinstance(expect, list) or not all(isinstance(e, str) and e for e in expect):
            raise QuestionFileError(f"{i}번째 항목: 'expect'는 글 id 문자열의 배열이어야 합니다(관련 글이 없어야 하면 빈 배열).")
        questions.append(Question(item["question"].strip(), list(dict.fromkeys(expect))))
    return questions


def rank_of(ranked_ids: Sequence[str], expect: Sequence[str]) -> Optional[int]:
    """정답 글들 중 가장 높은 순위(1부터). 하나도 없으면 None."""
    wanted = set(expect)
    for rank, post_id in enumerate(ranked_ids, 1):
        if post_id in wanted:
            return rank
    return None


def recall_at(ranks: Sequence[Optional[int]], k: int) -> float:
    return sum(1 for r in ranks if r is not None and r <= k) / len(ranks) if ranks else 0.0


def mrr(ranks: Sequence[Optional[int]]) -> float:
    return sum(1 / r for r in ranks if r is not None) / len(ranks) if ranks else 0.0


@dataclass
class QuestionResult:
    question: Question
    chunk_ranking: List[Tuple[str, float]]  # 조각 검색: (글 id, 가장 가까운 조각 점수) 점수순
    post_ranking: List[Tuple[str, float]]  # 예전 방식: (글 id, 글 벡터 점수) 점수순
    vector: List[float] = field(repr=False, default_factory=list)

    def rank(self, which: str) -> Optional[int]:
        ranking = self.chunk_ranking if which == "chunk" else self.post_ranking
        return rank_of([p for p, _ in ranking], self.question.expect)

    def top_score(self, which: str) -> float:
        ranking = self.chunk_ranking if which == "chunk" else self.post_ranking
        return ranking[0][1] if ranking else 0.0


def evaluate(questions: List[Question], store: EmbeddingStore, embedder: EmbedderClient, ids: List[str]) -> List[QuestionResult]:
    """질문마다 두 방식의 순위를 구한다. 임베딩 서비스를 쓸 수 없으면 EmbedderUnavailable."""
    results: List[QuestionResult] = []
    for q in questions:
        vector = embedder.embed([q.text]).embeddings[0]
        best: Dict[str, float] = {}
        for m in store.similar_chunks_to_vector(vector, ids, RANKING_DEPTH, 0.0):
            best.setdefault(m.post_id, m.score)  # 점수순으로 오므로 처음 나온 조각이 그 글의 가장 가까운 조각
        chunk_ranking = sorted(best.items(), key=lambda kv: -kv[1])
        post_ranking = [(m.post_id, m.score) for m in store.similar_to_vector(vector, ids, RANKING_DEPTH, 0.0)]
        results.append(QuestionResult(q, chunk_ranking, post_ranking, vector))
    return results


@dataclass(frozen=True)
class SweepRow:
    threshold: float
    shown_correct: float  # 정답이 있는 질문 중, 보여준 글에 정답이 들어 있는 비율
    shown_nothing: float  # 정답이 있는 질문 중, 아무 글도 보여주지 않은 비율(놓침)
    coverage: float  # 정답이 있는 질문에서 정답 글을 얼마나 빠짐없이 보여줬는가(평균). 정답이 여러 편이면 보여줄 수 있는 최대(limit)편 기준 — margin의 효과를 본다
    avg_extra: float  # 정답이 있는 질문에서 함께 보여준 정답 아닌 글의 평균 개수
    false_positive: float  # 관련 없는 질문 중, 글을 보여준(오탐) 비율


def sweep(
    results: List[QuestionResult],
    store: EmbeddingStore,
    ids: List[str],
    thresholds: Sequence[float],
    margin: float,
    limit: int = DEFAULT_LIMIT,
) -> List[SweepRow]:
    """기준 점수 후보마다, 실제 글 찾기(PostSearcher.search)와 같은 순서로 보여줄 글을 정해 본다(BE 호출만 뺀다)."""
    rows: List[SweepRow] = []
    positives = [r for r in results if not r.question.is_negative]
    negatives = [r for r in results if r.question.is_negative]
    for t in thresholds:
        searcher = PostSearcher(None, None, store, min_score=t, limit=limit, score_margin=margin)  # type: ignore[arg-type]
        floor = round(max(0.0, t - margin), 4)

        def shown(r: QuestionResult) -> List[str]:
            chunks = store.similar_chunks_to_vector(r.vector, ids, CHUNK_CANDIDATES, floor)
            legacy = store.similar_to_vector(r.vector, ids, limit, floor, True)
            return [h.post_id for h in searcher._select(group_hits(chunks, legacy, limit))]

        correct = nothing = extra = 0
        coverage = 0.0
        for r in positives:
            ids_shown = shown(r)
            expected = set(r.question.expect)
            correct += any(p in expected for p in ids_shown)
            coverage += len(expected & set(ids_shown)) / min(len(expected), limit)
            nothing += not ids_shown
            extra += sum(1 for p in ids_shown if p not in set(r.question.expect))
        false_pos = sum(1 for r in negatives if shown(r))
        n_pos, n_neg = len(positives), len(negatives)
        rows.append(
            SweepRow(
                threshold=t,
                shown_correct=correct / n_pos if n_pos else 0.0,
                shown_nothing=nothing / n_pos if n_pos else 0.0,
                coverage=coverage / n_pos if n_pos else 0.0,
                avg_extra=extra / n_pos if n_pos else 0.0,
                false_positive=false_pos / n_neg if n_neg else 0.0,
            )
        )
    return rows


def _rank_text(rank: Optional[int]) -> str:
    return "-" if rank is None else f"{rank}위"


def format_report(results: List[QuestionResult], rows: List[SweepRow], margin: float, candidate_count: int) -> str:
    out: List[str] = []
    positives = [r for r in results if not r.question.is_negative]
    negatives = [r for r in results if r.question.is_negative]
    out.append(f"질문 {len(results)}개 (정답 있음 {len(positives)}, 관련 글 없어야 함 {len(negatives)}) · 후보 글 {candidate_count}개 · margin {margin}")

    out.append("\n[질문별] 순위는 정답 글 중 가장 높은 순위, 점수는 해당 방식의 1등 점수")
    out.append("  종류 | 조각 순위 | 글벡터 순위 | 조각 1등 점수 | 글벡터 1등 점수 | 질문")
    for r in results:
        kind = "관련없음" if r.question.is_negative else "정답있음"
        q = r.question.text if len(r.question.text) <= 28 else r.question.text[:27] + "…"
        out.append(
            f"  {kind} | {_rank_text(r.rank('chunk')):>6} | {_rank_text(r.rank('post')):>8} | "
            f"{r.top_score('chunk'):.2f} | {r.top_score('post'):.2f} | {q}"
        )

    if positives:
        out.append("\n[정답이 있는 질문] 방식 비교")
        out.append("  방식 | Recall@1 | Recall@3 | MRR")
        for label, which in (("조각 검색", "chunk"), ("글 벡터(예전)", "post")):
            ranks = [r.rank(which) for r in positives]
            out.append(f"  {label} | {recall_at(ranks, 1):.2f} | {recall_at(ranks, 3):.2f} | {mrr(ranks):.2f}")
    if negatives:
        scores = sorted(r.top_score("chunk") for r in negatives)
        out.append(
            f"\n[관련 없는 질문] 조각 검색의 1등 점수 — 최소 {scores[0]:.2f} · 중앙 {scores[len(scores) // 2]:.2f} · 최대 {scores[-1]:.2f}"
            "\n  → 기준 점수를 최대값보다 높게 잡아야 이 질문들에서 오탐이 없다"
        )
    out.append("\n[기준 점수 스윕] 실제 글 찾기와 같은 선택 규칙")
    out.append("  기준 | 정답 보여줌 | 정답 포함률 | 놓침(없음) | 정답 외 글 평균 | 오탐(관련 없는데 보여줌)")
    for row in rows:
        out.append(
            f"  {row.threshold:.3f} | {row.shown_correct:>9.0%} | {row.coverage:>9.0%} | {row.shown_nothing:>8.0%} | {row.avg_extra:>11.2f} | {row.false_positive:>10.0%}"
        )
    if not positives or not negatives:
        out.append("\n⚠️ 정답 있는 질문과 관련 없는 질문이 모두 있어야 기준 점수를 정할 수 있다.")
    return "\n".join(out)


def fetch_ids() -> List[str]:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("select post_id from post_embedding union select post_id from post_chunk_embedding")
        return sorted(r[0] for r in cur.fetchall())


def parse_thresholds(raw: str) -> List[float]:
    try:
        values = [float(x) for x in raw.split(",") if x.strip()]
    except ValueError as e:
        raise ValueError("--thresholds는 0~1 사이 숫자를 쉼표로 이어서 줘야 합니다.") from e
    if not values or any(not 0.0 <= v <= 1.0 for v in values):
        raise ValueError("--thresholds는 0~1 사이 숫자를 쉼표로 이어서 줘야 합니다.")
    return values


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="글 찾기 검색 평가 (개발 DB + 로컬 임베딩 서비스)")
    parser.add_argument("--questions", default="eval/questions.json", help="질문 파일(JSON)")
    parser.add_argument("--thresholds", default=",".join(f"{t:.2f}" for t in DEFAULT_THRESHOLDS), help="스윕할 기준 점수 후보(쉼표)")
    parser.add_argument("--margin", type=float, default=None, help="1등과의 점수 차이 허용값(기본: POSTS_CHAT_SCORE_MARGIN 또는 0.09)")
    parser.add_argument("--allow-remote", action="store_true", help="개발 DB가 아닌 DB에서 실행할 때만")
    args = parser.parse_args(argv)

    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        print("DATABASE_URL을 환경변수로 줘야 합니다(.env는 읽지 않습니다).", file=sys.stderr)
        return 2
    try:
        target = check_target(database_url, args.allow_remote)
        thresholds = parse_thresholds(args.thresholds)
        questions = load_questions(args.questions)
    except (TargetNotAllowed, QuestionFileError, ValueError) as e:
        print(f"실행하지 않았습니다: {e}", file=sys.stderr)
        return 2
    margin = get_score_margin() if args.margin is None else args.margin
    embedder_url = os.environ.get("EMBEDDER_URL", "")
    print(f"대상: DB {target} → 임베딩 서비스 {embedder_url or '(없음)'}")

    store = EmbeddingStore()
    embedder = EmbedderClient(timeout=configured_timeout(), retries=configured_retries())
    ids = fetch_ids()
    known = set(ids)
    usable: List[Question] = []
    for q in questions:
        missing = [e for e in q.expect if e not in known]
        if missing:
            print(f"⚠️ 건너뜀(정답 id가 AI DB에 없음: 아직 임베딩되지 않은 글일 수 있음): {q.text[:30]}", file=sys.stderr)
            continue
        usable.append(q)
    if not usable:
        print("평가할 질문이 없습니다.", file=sys.stderr)
        return 2
    try:
        results = evaluate(usable, store, embedder, ids)
    except EmbedderUnavailable as e:
        print(f"임베딩 서비스를 쓸 수 없습니다: {e}", file=sys.stderr)
        return 2
    print(format_report(results, sweep(results, store, ids, thresholds, margin), margin, len(ids)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
