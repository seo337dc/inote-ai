"""글 목록 화면 채팅의 '글 찾기' — 질문으로 공개 글을 검색해 답변의 근거를 모은다.
설계: inote 레포 docs/kure/PLAN.md 6절 D의 1차 범위(결정 17).

규칙
- **공개 글만** 검색한다. 후보 글 id는 inote-server가 준다(BE가 공개 여부를 판정). 이 서비스는 공개 여부를 모른다.
  글 내용도 inote-server의 **공개 조회 API**로 가져온다 — 비공개·임시저장 글이면 BE가 404를 주므로 한 번 더 걸러진다.
- 어떤 실패(BE·임베딩 서비스·DB)도 예외로 새지 않는다 → status="unavailable"로 알리고, 호출한 쪽이 일반 답변으로 넘어간다.
- 로그에는 질문 내용을 남기지 않는다(개인적인 질문일 수 있다). 글 id와 오류 종류만 남긴다.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import List, Optional

import httpx

from app.services.embedder_client import EmbedderClient, get_embedder
from app.services.embedding_store import EmbeddingStore, get_store
from app.services.text_prep import html_to_text

logger = logging.getLogger(__name__)

# 질문과 글 전체를 비교하면 점수가 같은 주제여도 0.5~0.65대로 나온다(로컬 확인). A7 평가로 확정하기 전의 임시값.
DEFAULT_MIN_SCORE = 0.5
# 기준 점수에 못 미쳐도 1등 점수와 이만큼 이내면 같이 보여준다(같은 주제의 글인데 점수가 조금 낮은 경우).
# 로컬 확인: 모니터링 1편 0.55 / 2편 0.47(차이 0.08) / 관련 없는 글 0.44~0.46(차이 0.09 이상). A7 평가로 확정하기 전의 임시값.
DEFAULT_SCORE_MARGIN = 0.09
DEFAULT_LIMIT = 3
MAX_CHARS_PER_POST = 1500
SERVER_TIMEOUT = 5.0


def get_min_score() -> float:
    """POSTS_CHAT_MIN_SCORE(0~1). 없거나 범위를 벗어나면 기본값."""
    raw = os.environ.get("POSTS_CHAT_MIN_SCORE", "").strip()
    if not raw:
        return DEFAULT_MIN_SCORE
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_MIN_SCORE
    return value if 0.0 <= value <= 1.0 else DEFAULT_MIN_SCORE


def get_score_margin() -> float:
    """POSTS_CHAT_SCORE_MARGIN(0~1). 없거나 범위를 벗어나면 기본값. 0이면 1등과의 차이를 보지 않는다."""
    raw = os.environ.get("POSTS_CHAT_SCORE_MARGIN", "").strip()
    if not raw:
        return DEFAULT_SCORE_MARGIN
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_SCORE_MARGIN
    return value if 0.0 <= value <= 1.0 else DEFAULT_SCORE_MARGIN


@dataclass(frozen=True)
class FoundPost:
    id: str
    title: str
    category: str
    score: float
    text: str  # HTML을 걷어낸 본문(앞부분)


@dataclass(frozen=True)
class SearchOutcome:
    status: str  # "found" | "none"(관련 글 없음) | "unavailable"(검색을 못 함)
    posts: List[FoundPost]


class InoteServerClient:
    """inote-server 호출 — 공개 글 id 목록(내부 비밀키)과 공개 글 상세(공개 API)."""

    def __init__(self, base_url: Optional[str] = None, secret: Optional[str] = None):
        self._base = (base_url if base_url is not None else os.environ.get("INOTE_SERVER_URL", "")).rstrip("/")
        self._secret = secret if secret is not None else os.environ.get("INTERNAL_SECRET", "")

    async def public_post_ids(self) -> List[str]:
        async with httpx.AsyncClient(timeout=SERVER_TIMEOUT) as client:
            res = await client.get(f"{self._base}/blog/posts/public-ids", headers={"x-internal-secret": self._secret})
        res.raise_for_status()
        return [str(i) for i in res.json()["ids"]]

    async def get_post(self, post_id: str) -> Optional[dict]:
        """공개 글 상세. 볼 수 없는 글(비공개·임시저장·없음)이면 None."""
        async with httpx.AsyncClient(timeout=SERVER_TIMEOUT) as client:
            res = await client.get(f"{self._base}/blog/posts/{post_id}")
        if res.status_code == 404:
            return None
        res.raise_for_status()
        return res.json()


def get_server_client() -> InoteServerClient:
    """FastAPI 의존성 — 테스트에서 dependency_overrides로 가짜를 넣는다."""
    return InoteServerClient()


class PostSearcher:
    def __init__(
        self,
        server: InoteServerClient,
        embedder: EmbedderClient,
        store: EmbeddingStore,
        min_score: Optional[float] = None,
        limit: int = DEFAULT_LIMIT,
        score_margin: Optional[float] = None,
    ):
        self._server = server
        self._embedder = embedder
        self._store = store
        self._min_score = get_min_score() if min_score is None else min_score
        self._limit = limit
        self._margin = get_score_margin() if score_margin is None else score_margin

    async def search(self, query: str) -> SearchOutcome:
        try:
            ids = await self._server.public_post_ids()
            if not ids:
                return SearchOutcome("none", [])
            # KURE 호출과 DB 조회는 동기 코드라 스레드에서 돌려 이벤트 루프를 막지 않는다
            embedded = await asyncio.to_thread(self._embedder.embed, [query])
            # 기준보다 낮게 한도를 풀어서 가져온 뒤, 1등이 기준을 넘을 때만 아래에서 다시 거른다
            floor = round(max(0.0, self._min_score - self._margin), 4)
            candidates = await asyncio.to_thread(
                self._store.similar_to_vector, embedded.embeddings[0], ids, self._limit, floor
            )
            matches = self._select(candidates)
            if not matches:
                return SearchOutcome("none", [])
            details = await asyncio.gather(
                *[self._server.get_post(m.post_id) for m in matches], return_exceptions=True
            )
        except Exception as e:  # BE·임베딩 서비스·DB 어느 쪽이든 — 질문 내용은 남기지 않는다
            logger.warning("post search unavailable: %s", type(e).__name__)
            return SearchOutcome("unavailable", [])

        found: List[FoundPost] = []
        for match, detail in zip(matches, details):
            if isinstance(detail, Exception):
                logger.warning("failed to load post %s: %s", match.post_id, type(detail).__name__)
                continue
            # 공개 조회 API가 비공개 글을 404로 막지만, 응답에 비공개 표시가 있으면 한 번 더 버린다
            if detail is None or detail.get("isPrivate") is True:
                continue
            found.append(
                FoundPost(
                    id=match.post_id,
                    title=str(detail.get("title") or ""),
                    category=str(detail.get("category") or ""),
                    score=round(match.score, 4),
                    text=html_to_text(str(detail.get("content") or ""))[:MAX_CHARS_PER_POST],
                )
            )
        return SearchOutcome("found" if found else "none", found)


    def _select(self, candidates: List) -> List:
        """1등이 기준 점수를 넘지 못하면 관련 글 없음. 넘으면 기준을 넘은 글과, 1등과의 차이가 margin 이내인 글을 남긴다."""
        if not candidates:
            return []
        top = max(m.score for m in candidates)
        if top < self._min_score:
            return []
        keep = [m for m in candidates if m.score >= self._min_score or m.score >= top - self._margin]
        return sorted(keep, key=lambda m: m.score, reverse=True)


def get_searcher() -> PostSearcher:
    """FastAPI 의존성 — 테스트에서 dependency_overrides로 통째로 바꾼다."""
    return PostSearcher(get_server_client(), get_embedder(), get_store())
