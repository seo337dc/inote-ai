"""글 하나의 벡터(글 전체 + 조각들)를 만들고 저장하는 공통 로직 — 글 저장 API(PUT)와 대기열 워커가 같은 규칙을 쓰도록 한 곳에 둔다.

글 벡터(post_embedding)는 제목+본문 앞 1,500자 하나(유사 글 추천용), 조각 벡터(post_chunk_embedding)는 글 전체를 나눈 조각마다 하나(RAG 검색용)다.
다시 계산할지는 따로 정한다: 글 벡터는 (모델, 앞 1,500자 해시), 조각은 (모델, 글 전체+나누기 설정 해시)가 같으면 건너뛴다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from app.services.chunking import CHUNKER_VERSION, MAX_CHARS, OVERLAP_CHARS, TARGET_CHARS, Chunk, build_chunk_embedding_text, chunk_text
from app.services.embedder_client import EmbedderClient, EmbedderUnavailable, expected_model
from app.services.embedding_store import ChunkRow, EmbeddingStore, write_with_cursor
from app.services.text_prep import build_embedding_text, content_hash, html_to_text

MAX_BATCH = 16  # 임베딩 서비스가 한 번에 받는 입력 수 상한(inote-embedder 기본값)


@dataclass(frozen=True)
class PreparedPost:
    post_text: str  # 글 벡터용 텍스트(제목 + 본문 앞부분)
    post_digest: str
    source_hash: str  # 글 전체(제목+본문 텍스트+나누기 설정)의 해시 — 조각을 다시 만들지 정하는 기준
    chunks: List[Chunk]
    chunk_texts: List[str]  # 조각마다 제목을 붙인 임베딩용 텍스트


@dataclass(frozen=True)
class Plan:
    post: bool  # 글 벡터를 새로 만들어야 하는가
    chunks: bool  # 조각을 새로 만들어야 하는가

    @property
    def nothing(self) -> bool:
        return not self.post and not self.chunks


@dataclass(frozen=True)
class EmbeddedPost:
    model: str
    post_vector: Optional[List[float]]
    chunk_vectors: Optional[List[List[float]]]  # None이면 조각을 새로 만들지 않았다


def prepare_post(title: str, content_html: str) -> Optional[PreparedPost]:
    """제목·본문(HTML) → 임베딩할 텍스트들. 제목·본문이 모두 비었으면 None. 네트워크·DB를 쓰지 않는다."""
    plain = html_to_text(content_html)
    post_text = build_embedding_text(title, plain)
    if not post_text:
        return None
    chunks = chunk_text(plain)
    source_hash = content_hash(f"chunks:v{CHUNKER_VERSION}:{TARGET_CHARS}:{MAX_CHARS}:{OVERLAP_CHARS}\n{title.strip()}\n{plain}")
    return PreparedPost(
        post_text=post_text,
        post_digest=content_hash(post_text),
        source_hash=source_hash,
        chunks=chunks,
        chunk_texts=[build_chunk_embedding_text(title, c.text) for c in chunks],
    )


def plan_for(store: EmbeddingStore, post_id: str, prepared: PreparedPost) -> Plan:
    """저장된 상태와 비교해 무엇을 다시 만들어야 하는지 정한다."""
    model = expected_model()
    need_post = store.get_state(post_id) != (model, prepared.post_digest)
    chunk_state = store.get_chunk_state(post_id)
    if prepared.chunks:
        need_chunks = chunk_state != (model, prepared.source_hash, len(prepared.chunks))
    else:
        need_chunks = chunk_state is not None  # 조각이 하나도 없어야 하는데 옛 조각이 남아 있으면 지운다
    return Plan(post=need_post, chunks=need_chunks)


def embed_prepared(embedder: EmbedderClient, prepared: PreparedPost, plan: Plan) -> EmbeddedPost:
    """plan이 가리키는 텍스트만 임베딩한다. 입력이 많으면 MAX_BATCH개씩 나눠 보낸다."""
    texts: List[str] = []
    if plan.post:
        texts.append(prepared.post_text)
    if plan.chunks:
        texts.extend(prepared.chunk_texts)

    vectors: List[List[float]] = []
    model: Optional[str] = None
    for i in range(0, len(texts), MAX_BATCH):
        result = embedder.embed(texts[i : i + MAX_BATCH])
        if model is not None and result.model != model:
            raise EmbedderUnavailable("임베딩 서비스가 요청 도중 다른 모델로 응답했습니다.")
        model = result.model
        vectors.extend(result.embeddings)

    post_vector = vectors[0] if plan.post else None
    chunk_vectors = vectors[1 if plan.post else 0 :] if plan.chunks else None
    return EmbeddedPost(model=model or expected_model(), post_vector=post_vector, chunk_vectors=chunk_vectors)


def write_args(prepared: PreparedPost, embedded: EmbeddedPost) -> Tuple[Optional[Tuple[str, List[float]]], Optional[List[ChunkRow]]]:
    """embed_prepared의 결과 → 저장소에 넘길 (글 벡터, 조각 목록). 만들지 않은 것은 None(저장소가 건드리지 않는다)."""
    post = (prepared.post_digest, embedded.post_vector) if embedded.post_vector is not None else None
    chunks: Optional[List[ChunkRow]] = None
    if embedded.chunk_vectors is not None:
        chunks = [
            ChunkRow(idx=c.index, start=c.start, end=c.end, digest=content_hash(t), vector=v)
            for c, t, v in zip(prepared.chunks, prepared.chunk_texts, embedded.chunk_vectors)
        ]
    return post, chunks


def write_embedded(cur, post_id: str, prepared: PreparedPost, embedded: EmbeddedPost) -> None:
    """embed_prepared의 결과를 이미 열려 있는 트랜잭션(cursor)으로 쓴다 — 워커가 '작업 완료 처리'와 묶으려고 쓴다."""
    post, chunks = write_args(prepared, embedded)
    write_with_cursor(cur, post_id, embedded.model, post, chunks, prepared.source_hash)
