"""post_embedding 테이블(AI DB) 접근 — inote 레포 docs/kure/PLAN.md 5.3.

핵심 규칙: **유사 글 조회는 호출한 쪽(inote-server)이 넘긴 후보 id 안에서만** 한다. 후보 밖의 글은 아무리 가까워도 나오지 않는다.
이 테이블에는 공개 여부·작성자가 없다 — 공개 범위는 항상 후보 id로 BE가 정한다.

연산자는 `operator(extensions.<=>)`로 스키마까지 붙여 쓴다 — 접속 계정의 search_path에 extensions가 있는지에 의존하지 않는다
(Supabase 운영 DB의 search_path는 아직 확인 전이라, 없어도 동작하게 한다).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from app.db.postgres import get_connection


@dataclass(frozen=True)
class Match:
    post_id: str
    score: float  # 코사인 유사도(1 - 코사인 거리), 클수록 비슷하다


def vector_literal(vector: Sequence[float]) -> str:
    """파이썬 숫자 목록 → pgvector 입력 문자열 '[0.1,0.2,...]' (%s::vector로 넘긴다)."""
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


class EmbeddingStore:
    def get_state(self, post_id: str) -> Optional[Tuple[str, str]]:
        """저장된 (모델, 내용 해시). 벡터가 없으면 None."""
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("select model, content_hash from post_embedding where post_id = %s", (post_id,))
            row = cur.fetchone()
        return (row[0], row[1]) if row else None

    def upsert(self, post_id: str, model: str, digest: str, vector: Sequence[float]) -> None:
        with get_connection() as conn, conn.cursor() as cur:
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

    def delete(self, post_id: str) -> bool:
        """지웠으면 True, 원래 없었으면 False (어느 쪽이든 오류가 아니다)."""
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("delete from post_embedding where post_id = %s", (post_id,))
            return cur.rowcount > 0

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
        self, vector: Sequence[float], candidate_ids: Sequence[str], limit: int, min_score: float
    ) -> List[Match]:
        """아직 저장되지 않은 글(쓰는 중인 글)의 벡터와 가까운 후보를 찾는다."""
        literal = vector_literal(vector)
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                select post_id, 1 - (embedding operator(extensions.<=>) %s::extensions.vector) as score
                from post_embedding
                where post_id = any(%s) and 1 - (embedding operator(extensions.<=>) %s::extensions.vector) >= %s
                order by embedding operator(extensions.<=>) %s::extensions.vector
                limit %s
                """,
                (literal, list(candidate_ids), literal, min_score, literal, limit),
            )
            return [Match(r[0], float(r[1])) for r in cur.fetchall()]


def get_store() -> EmbeddingStore:
    """FastAPI 의존성 — 테스트에서 dependency_overrides로 가짜를 넣는다."""
    return EmbeddingStore()
