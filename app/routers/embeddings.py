"""글 임베딩 API — inote-server(BE) 전용 내부 엔드포인트. 설계: inote 레포 docs/kure/PLAN.md 4·5·6절.

역할 분담
- inote-server가 글 저장·삭제 때 이 API를 부르고(실패해도 글 저장은 막지 않는다), 유사 글을 찾을 때는 **자기가 정한 후보 글 id 목록**을 넘긴다.
- 이 서비스는 글의 공개 여부를 모른다. 후보 id **안에서만** 순위를 매겨 돌려준다 → 후보 밖의 글이 나올 수 없다.
- 임베딩 서비스(KURE)를 부르는 것도 이 서비스뿐이다. 유사 글 조회는 임베딩 서비스를 쓸 수 없으면 503을 돌려주고, 호출한 쪽이 기능을 숨긴다(fail-open).
- 글 저장(PUT)은 임베딩 서비스를 쓸 수 없으면 **작업 대기열(embedding_job)에 남기고 202(queued)**로 답한다 → 나중에 워커가 처리한다.
  대기열에도 남기지 못하면 그때만 503. 임베딩에 성공하면 그 글의 대기 작업은 지운다(더 오래된 내용이 나중에 덮어쓰지 않게).

엔드포인트 (모두 헤더 x-internal-secret 필요)
  PUT    /embeddings/posts/{post_id}   글 제목·HTML 본문 → 벡터를 만들어 저장 (내용·모델이 같으면 다시 계산하지 않는다). 임베딩 서비스가 안 되면 대기열에 남기고 202
  DELETE /embeddings/posts/{post_id}   벡터와 대기 작업 삭제 (없어도 오류가 아니다)
  POST   /embeddings/similar           후보 id 안에서 가까운 글 조회 (저장된 글 기준 postId, 또는 저장 전 텍스트 title+content 기준)
"""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from app.services.embedder_client import EXPECTED_DIM, EmbedderClient, EmbedderUnavailable, expected_model, get_embedder
from app.services.embedding_jobs import EmbeddingJobStore, get_job_store
from app.services.embedding_store import EmbeddingStore, get_store
from app.services.internal_auth import require_internal_secret
from app.services.post_vectors import embed_prepared, plan_for, prepare_post, write_args
from app.services.text_prep import build_embedding_text, html_to_text

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/embeddings", tags=["embeddings"], dependencies=[Depends(require_internal_secret)])

# 요청 크기 상한 — 비정상적으로 큰 요청으로 메모리·시간을 쓰지 못하게 한다
MAX_TITLE_CHARS = 500
MAX_CONTENT_CHARS = 2_000_000
MAX_CANDIDATES = 5000  # 글이 수천 개를 넘으면 "AI가 상위 N개 → BE가 거름" 방식으로 바꾸는 것을 검토(PLAN 5.6)
MAX_LIMIT = 50


class UpsertRequest(BaseModel):
    title: str = Field(max_length=MAX_TITLE_CHARS)
    content: str = Field(max_length=MAX_CONTENT_CHARS)  # HTML 본문


class SimilarRequest(BaseModel):
    # 기준은 둘 중 하나: 이미 저장된 글(postId) 또는 아직 저장 전인 글(title+content, 쓰는 중인 글)
    postId: Optional[str] = Field(default=None, min_length=1)
    title: Optional[str] = Field(default=None, max_length=MAX_TITLE_CHARS)
    content: Optional[str] = Field(default=None, max_length=MAX_CONTENT_CHARS)
    # BE가 정한 "이 요청자가 볼 수 있는 글 id" — 이 안에서만 찾는다 (비어 있으면 아무것도 돌려주지 않는다)
    candidateIds: List[str] = Field(max_length=MAX_CANDIDATES)
    limit: int = Field(default=5, ge=1, le=MAX_LIMIT)
    minScore: float = Field(default=0.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def exactly_one_source(self) -> "SimilarRequest":
        has_post = self.postId is not None
        has_text = self.title is not None or self.content is not None
        if has_post == has_text:
            raise ValueError("postId 또는 title+content 중 하나만 보내야 합니다.")
        if has_text and (self.title is None or self.content is None):
            raise ValueError("텍스트로 조회하려면 title과 content를 모두 보내야 합니다.")
        return self


class SimilarItem(BaseModel):
    postId: str
    score: float


class SimilarResponse(BaseModel):
    items: List[SimilarItem]
    # postId 기준일 때 그 글의 벡터가 저장돼 있는지(없으면 items는 비어 있다). 텍스트 기준이면 null.
    sourceEmbedded: Optional[bool] = None


def _embedding_text(title: str, content_html: str) -> str:
    return build_embedding_text(title, html_to_text(content_html))


def _unavailable(e: EmbedderUnavailable) -> HTTPException:
    return HTTPException(status_code=503, detail=str(e))


def _queue_or_unavailable(jobs: EmbeddingJobStore, post_id: str, body: UpsertRequest, cause: EmbedderUnavailable):
    """임베딩 서비스를 쓸 수 없을 때: 작업을 대기열에 남기고 202. 대기열에도 남기지 못하면 503(예전과 같은 응답)."""
    try:
        version = jobs.enqueue(post_id, body.title, body.content)
    except Exception as e:  # DB 오류 등 — 글 내용은 로그에 남기지 않는다
        logger.warning("embedding job enqueue failed (post %s): %s", post_id, type(e).__name__)
        raise _unavailable(cause)
    logger.info("embedding job queued (post %s, version %s)", post_id, version)
    return JSONResponse(status_code=202, content={"status": "queued", "reason": str(cause)})


def _discard_job(jobs: EmbeddingJobStore, post_id: str) -> None:
    """이 요청의 내용이 가장 최신이므로, 그 글의 더 오래된 대기 작업은 필요 없다. 실패해도 저장 응답에는 영향이 없다."""
    try:
        jobs.delete(post_id)
    except Exception as e:
        logger.warning("embedding job cleanup failed (post %s): %s", post_id, type(e).__name__)


# def(동기) 함수로 둬서 DB·HTTP 호출이 이벤트 루프를 막지 않고 스레드풀에서 돈다
@router.put("/posts/{post_id}")
def upsert_post_embedding(
    post_id: str,
    body: UpsertRequest,
    store: EmbeddingStore = Depends(get_store),
    embedder: EmbedderClient = Depends(get_embedder),
    jobs: EmbeddingJobStore = Depends(get_job_store),
):
    prepared = prepare_post(body.title, body.content)
    if prepared is None:
        raise HTTPException(status_code=422, detail="임베딩할 내용이 없습니다.")

    # 내용(해시)과 모델이 같으면 임베딩 서비스를 부르지 않는다 — 같은 글을 다시 저장해도 비용이 들지 않는다.
    # 글 벡터와 조각은 따로 비교해서, 바뀐 쪽만 다시 만든다(예: 조각 기능을 켜기 전에 저장된 글은 조각만 새로 만든다)
    plan = plan_for(store, post_id, prepared)
    if plan.nothing:
        _discard_job(jobs, post_id)
        return {"status": "unchanged", "model": expected_model(), "chunks": len(prepared.chunks)}

    try:
        embedded = embed_prepared(embedder, prepared, plan)
    except EmbedderUnavailable as e:
        return _queue_or_unavailable(jobs, post_id, body, e)

    # 벡터를 쓰기 **전에** 대기 작업을 지운다: 워커가 같은 글을 처리 중이면 그 완료(행 잠금)를 기다렸다가 지우고,
    # 그 뒤에 이 최신 벡터를 쓴다 → 워커의 오래된 벡터가 이 벡터를 덮어쓸 수 없다. (지운 뒤에 워커가 오면 작업이 없어 아무것도 쓰지 않는다)
    _discard_job(jobs, post_id)
    post, chunks = write_args(prepared, embedded)
    store.write(post_id, embedded.model, post, chunks, prepared.source_hash)  # 글 벡터와 조각을 한 트랜잭션으로
    return {"status": "embedded", "model": embedded.model, "dim": EXPECTED_DIM, "chunks": len(prepared.chunks)}


@router.delete("/posts/{post_id}")
def delete_post_embedding(
    post_id: str,
    store: EmbeddingStore = Depends(get_store),
    jobs: EmbeddingJobStore = Depends(get_job_store),
):
    # 없어도 오류가 아니다 — 글 삭제 때 BE가 부르는 정리용이라 여러 번 불려도 안전해야 한다.
    # 대기 작업에는 글 본문이 들어 있으므로 삭제한 글의 내용이 남지 않게 함께 지운다(실패하면 숨기지 않고 오류로 알린다).
    job_deleted = jobs.delete(post_id)
    return {"deleted": store.delete(post_id), "jobDeleted": job_deleted}


@router.post("/similar", response_model=SimilarResponse)
def similar(
    body: SimilarRequest,
    store: EmbeddingStore = Depends(get_store),
    embedder: EmbedderClient = Depends(get_embedder),
):
    candidates = list(dict.fromkeys(body.candidateIds))  # 중복 제거(순서 유지)
    if not candidates:
        # 볼 수 있는 글이 없으면 찾을 것도 없다 — 임베딩 서비스를 부르지 않는다
        return SimilarResponse(items=[])

    if body.postId is not None:
        matches = store.similar_to_post(body.postId, candidates, body.limit, body.minScore)
        if matches is None:
            return SimilarResponse(items=[], sourceEmbedded=False)
        return SimilarResponse(items=_items(matches), sourceEmbedded=True)

    text = _embedding_text(body.title or "", body.content or "")
    if not text:
        raise HTTPException(status_code=422, detail="조회할 내용이 없습니다.")
    try:
        result = embedder.embed([text])
    except EmbedderUnavailable as e:
        raise _unavailable(e)
    matches = store.similar_to_vector(result.embeddings[0], candidates, body.limit, body.minScore)
    return SimilarResponse(items=_items(matches))


def _items(matches) -> List[SimilarItem]:
    return [SimilarItem(postId=m.post_id, score=round(m.score, 4)) for m in matches]
