"""post_embedding 테이블(AI DB) 접근 — inote 레포 docs/kure/PLAN.md 5.3.

핵심 규칙: **유사 글 조회는 호출한 쪽(inote-server)이 넘긴 후보 id 안에서만** 한다. 후보 밖의 글은 아무리 가까워도 나오지 않는다.
이 테이블에는 공개 여부·작성자가 없다 — 공개 범위는 항상 후보 id로 BE가 정한다.

연산자는 `operator(extensions.<=>)`로 스키마까지 붙여 쓴다 — 접속 계정의 search_path에 extensions가 있는지에 의존하지 않는다
(Supabase 운영 DB의 search_path는 아직 확인 전이라, 없어도 동작하게 한다).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from app.db.postgres import get_connection


@dataclass(frozen=True)
class Match:
    post_id: str
    score: float  # 코사인 유사도(1 - 코사인 거리), 클수록 비슷하다


@dataclass(frozen=True)
class ChunkMatch:
    post_id: str
    idx: int
    score: float  # 질문과 이 조각의 코사인 유사도


@dataclass(frozen=True)
class ChunkInfo:
    """조각의 위치와 해시(본문 없음) — 원문에서 다시 잘라 낼 때 쓰고, 해시로 원문이 바뀌지 않았는지 확인한다."""

    idx: int
    start: int
    end: int
    digest: str


@dataclass(frozen=True)
class ChunkRow:
    """post_chunk_embedding에 넣을 조각 하나 — 본문은 넣지 않고 위치(start, end)와 해시만 둔다."""

    idx: int
    start: int
    end: int
    digest: str
    vector: Sequence[float]


def vector_literal(vector: Sequence[float]) -> str:
    """파이썬 숫자 목록 → pgvector 입력 문자열 '[0.1,0.2,...]' (%s::vector로 넘긴다)."""
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def upsert_with_cursor(cur, post_id: str, model: str, digest: str, vector: Sequence[float]) -> None:
    """이미 열려 있는 트랜잭션(cursor) 안에서 벡터를 저장한다 — 대기열 워커가 '작업 완료 처리'와 한 트랜잭션으로 묶으려고 쓴다."""
    cur.execute(
        """
        insert into post_embedding (post_id, model, content_hash, embedding)
        values (%s, %s, %s, %s::extensions.vector)
        on conflict (post_id) do update set
          model = excluded.model, content_hash = excluded.content_hash,
          embedding = excluded.embedding, embedded_at = now()
        """,
        (post_id, model, digest, vector_literal(vector)),
    )


def write_with_cursor(
    cur,
    post_id: str,
    model: str,
    post: Optional[Tuple[str, Sequence[float]]],
    chunks: Optional[Sequence[ChunkRow]],
    source_hash: str,
) -> None:
    """한 트랜잭션(cursor) 안에서 글 벡터와 조각 벡터를 함께 쓴다 — 중간에 실패하면 모두 되돌아가 "일부만 새 조각"인 상태가 생기지 않는다.

    post=(content_hash, vector): 글 벡터를 저장한다. None이면 건드리지 않는다.
    chunks: 그 글의 조각을 **모두 지우고** 이 목록으로 바꾼다(빈 목록이면 조각을 모두 지운다). None이면 건드리지 않는다."""
    if post is not None:
        upsert_with_cursor(cur, post_id, model, post[0], post[1])
    if chunks is not None:
        cur.execute("delete from post_chunk_embedding where post_id = %s", (post_id,))
        for c in chunks:
            cur.execute(
                """
                insert into post_chunk_embedding
                  (post_id, idx, start_offset, end_offset, content_hash, source_hash, model, embedding)
                values (%s, %s, %s, %s, %s, %s, %s, %s::extensions.vector)
                """,
                (post_id, c.idx, c.start, c.end, c.digest, source_hash, model, vector_literal(c.vector)),
            )


class EmbeddingStore:
    def get_state(self, post_id: str) -> Optional[Tuple[str, str]]:
        """저장된 (모델, 내용 해시). 벡터가 없으면 None."""
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("select model, content_hash from post_embedding where post_id = %s", (post_id,))
            row = cur.fetchone()
        return (row[0], row[1]) if row else None

    def get_chunk_state(self, post_id: str) -> Optional[Tuple[str, str, int]]:
        """저장된 조각의 (모델, 글 전체 해시, 조각 수). 조각이 없으면 None. 한 글의 조각은 한 번에 통째로 바뀌므로 값이 하나뿐이어야 한다.
        (모델·해시가 섞여 있으면 중간 상태이므로 일치하지 않게 ("", "", 수)를 돌려줘 다시 만들게 한다.)"""
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "select min(model), max(model), min(source_hash), max(source_hash), count(*) from post_chunk_embedding where post_id = %s",
                (post_id,),
            )
            lo_model, hi_model, lo_hash, hi_hash, count = cur.fetchone()
        if not count:
            return None
        if lo_model != hi_model or lo_hash != hi_hash:
            return ("", "", int(count))
        return (lo_model, lo_hash, int(count))

    def upsert(self, post_id: str, model: str, digest: str, vector: Sequence[float]) -> None:
        with get_connection() as conn, conn.cursor() as cur:
            upsert_with_cursor(cur, post_id, model, digest, vector)

    def write(
        self,
        post_id: str,
        model: str,
        post: Optional[Tuple[str, Sequence[float]]],
        chunks: Optional[Sequence[ChunkRow]],
        source_hash: str,
    ) -> None:
        """글 벡터와 조각 벡터를 한 트랜잭션으로 쓴다(write_with_cursor 참고)."""
        with get_connection() as conn, conn.cursor() as cur:
            write_with_cursor(cur, post_id, model, post, chunks, source_hash)

    def delete(self, post_id: str) -> bool:
        """글 벡터와 조각 벡터를 지웠으면 True, 원래 없었으면 False (어느 쪽이든 오류가 아니다)."""
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("delete from post_chunk_embedding where post_id = %s", (post_id,))
            chunks_deleted = cur.rowcount > 0
            cur.execute("delete from post_embedding where post_id = %s", (post_id,))
            return cur.rowcount > 0 or chunks_deleted

    def similar_to_post(
        self, post_id: str, candidate_ids: Sequence[str], limit: int, min_score: float
    ) -> Optional[List[Match]]:
        """이미 저장된 글의 벡터와 가까운 후보를 찾는다. 그 글의 벡터가 없으면 None. 자기 자신은 후보에 있어도 뺀다."""
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("select 1 from post_embedding where post_id = %s", (post_id,))
            if cur.fetchone() is None:
                return None
            cur.execute(
                """
                select b.post_id, 1 - (b.embedding operator(extensions.<=>) a.embedding) as score
                from post_embedding a
                join post_embedding b on b.post_id = any(%s) and b.post_id <> a.post_id
                where a.post_id = %s and 1 - (b.embedding operator(extensions.<=>) a.embedding) >= %s
                order by b.embedding operator(extensions.<=>) a.embedding
                limit %s
                """,
                (list(candidate_ids), post_id, min_score, limit),
            )
            return [Match(r[0], float(r[1])) for r in cur.fetchall()]

    def similar_to_vector(
        self,
        vector: Sequence[float],
        candidate_ids: Sequence[str],
        limit: int,
        min_score: float,
        without_chunks_only: bool = False,
    ) -> List[Match]:
        """아직 저장되지 않은 글(쓰는 중인 글)의 벡터와 가까운 후보를 찾는다.
        without_chunks_only=True면 조각이 아직 없는 글만 본다(조각 검색이 못 보는 글을 글 벡터로 보충할 때)."""
        literal = vector_literal(vector)
        no_chunks = (
            " and not exists (select 1 from post_chunk_embedding c where c.post_id = post_embedding.post_id)"
            if without_chunks_only
            else ""
        )
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                select post_id, 1 - (embedding operator(extensions.<=>) %s::extensions.vector) as score
                from post_embedding
                where post_id = any(%s) and 1 - (embedding operator(extensions.<=>) %s::extensions.vector) >= %s{no_chunks}
                order by embedding operator(extensions.<=>) %s::extensions.vector
                limit %s
                """,
                (literal, list(candidate_ids), literal, min_score, literal, limit),
            )
            return [Match(r[0], float(r[1])) for r in cur.fetchall()]

    def similar_chunks_to_vector(
        self, vector: Sequence[float], candidate_ids: Sequence[str], limit: int, min_score: float
    ) -> List[ChunkMatch]:
        """질문 벡터와 가까운 조각을 찾는다. **후보 id 안의 글의 조각만** 본다(공개 범위는 후보 id로 정해진다)."""
        literal = vector_literal(vector)
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                select post_id, idx, 1 - (embedding operator(extensions.<=>) %s::extensions.vector) as score
                from post_chunk_embedding
                where post_id = any(%s) and 1 - (embedding operator(extensions.<=>) %s::extensions.vector) >= %s
                order by embedding operator(extensions.<=>) %s::extensions.vector
                limit %s
                """,
                (literal, list(candidate_ids), literal, min_score, literal, limit),
            )
            return [ChunkMatch(r[0], int(r[1]), float(r[2])) for r in cur.fetchall()]

    def chunk_layout(self, post_ids: Sequence[str]) -> Dict[str, List[ChunkInfo]]:
        """글들의 조각 위치·해시(idx 순). 앞뒤 조각을 함께 가져오고 원문이 바뀌지 않았는지 확인하는 데 쓴다."""
        layout: Dict[str, List[ChunkInfo]] = {}
        if not post_ids:
            return layout
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "select post_id, idx, start_offset, end_offset, content_hash from post_chunk_embedding where post_id = any(%s) order by post_id, idx",
                (list(post_ids),),
            )
            for post_id, idx, start, end, digest in cur.fetchall():
                layout.setdefault(post_id, []).append(ChunkInfo(int(idx), int(start), int(end), digest))
        return layout

    def chunk_summary(self, post_ids: Sequence[str]) -> Dict[str, Tuple[int, datetime]]:
        """글별 (조각 수, 가장 최근 임베딩 시각). 조각이 없는 글은 결과에 없다. 읽기 전용(관리자 상태 조회용)."""
        summary: Dict[str, Tuple[int, datetime]] = {}
        if not post_ids:
            return summary
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "select post_id, count(*), max(embedded_at) from post_chunk_embedding where post_id = any(%s) group by post_id",
                (list(post_ids),),
            )
            for post_id, count, embedded_at in cur.fetchall():
                summary[post_id] = (int(count), embedded_at)
        return summary


def get_store() -> EmbeddingStore:
    """FastAPI 의존성 — 테스트에서 dependency_overrides로 가짜를 넣는다."""
    return EmbeddingStore()
