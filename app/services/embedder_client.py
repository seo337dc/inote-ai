"""임베딩 서비스(inote-embedder) 호출 클라이언트.

서비스 계약: POST {EMBEDDER_URL}/embed  헤더 x-internal-secret  {"inputs": [...]}
            → {"model": "...", "dim": 1024, "embeddings": [[...], ...]}  (정규화된 벡터)

환경 변수
- EMBEDDER_URL    임베딩 서비스 주소. 없으면 임베딩이 필요한 요청은 503(사용할 수 없음)으로 거절된다.
- EMBEDDER_SECRET 임베딩 서비스가 기대하는 비밀키. BE↔AI의 INTERNAL_SECRET과는 **다른 값**을 쓴다(서로 노출되어도 영향이 번지지 않게).
- EMBEDDER_MODEL  기대하는 모델 이름(기본 nlpai-lab/KURE-v1) — 저장된 벡터와 모델이 다르면 다시 계산하는 기준.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)

EXPECTED_DIM = 1024  # KURE-v1 (모델 카드로 확인됨). post_embedding.embedding vector(1024)와 같아야 한다.
DEFAULT_MODEL = "nlpai-lab/KURE-v1"
# Vercel Hobby 함수는 기본·최대 300초(문서 확인, 2026-10-12). 글 한 편 임베딩은 3~8초라 넉넉하되, 임베딩 서비스가 멎었을 때 오래 붙들리지 않게 상한을 둔다.
DEFAULT_TIMEOUT = 40.0


class EmbedderUnavailable(Exception):
    """임베딩 서비스를 쓸 수 없다(주소 없음, 연결 실패, 시간 초과, 이상한 응답). 호출한 쪽은 기능을 건너뛰는(fail-open) 용도로 쓴다."""


@dataclass(frozen=True)
class EmbedResult:
    model: str
    dim: int
    embeddings: List[List[float]]


def expected_model() -> str:
    return os.environ.get("EMBEDDER_MODEL", DEFAULT_MODEL)


class EmbedderClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        secret: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        transport: Optional[httpx.BaseTransport] = None,  # 테스트에서 가짜 서버를 끼운다
    ) -> None:
        self._base_url = (base_url if base_url is not None else os.environ.get("EMBEDDER_URL", "")).rstrip("/")
        self._secret = secret if secret is not None else os.environ.get("EMBEDDER_SECRET", "")
        self._timeout = timeout
        self._transport = transport

    def embed(self, texts: List[str]) -> EmbedResult:
        if not self._base_url:
            raise EmbedderUnavailable("EMBEDDER_URL이 설정되지 않았습니다.")
        try:
            with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
                res = client.post(
                    f"{self._base_url}/embed",
                    json={"inputs": texts},
                    headers={"x-internal-secret": self._secret},
                )
        except httpx.HTTPError as e:
            logger.warning("embedder request failed: %s", type(e).__name__)
            raise EmbedderUnavailable(f"임베딩 서비스 요청 실패: {type(e).__name__}") from e

        if res.status_code != 200:
            logger.warning("embedder returned status %s", res.status_code)
            raise EmbedderUnavailable(f"임베딩 서비스가 {res.status_code}를 돌려줬습니다.")

        try:
            body = res.json()
            model, dim, embeddings = body["model"], int(body["dim"]), body["embeddings"]
        except (ValueError, KeyError, TypeError) as e:
            raise EmbedderUnavailable("임베딩 서비스 응답 형식이 올바르지 않습니다.") from e

        if dim != EXPECTED_DIM or len(embeddings) != len(texts) or any(len(v) != dim for v in embeddings):
            # 차원이 다른 벡터를 저장하면 DB가 거절하거나 비교가 무의미해진다 — 여기서 막는다
            raise EmbedderUnavailable(f"임베딩 응답이 예상과 다릅니다(dim={dim}, 개수={len(embeddings)}).")
        return EmbedResult(model=model, dim=dim, embeddings=embeddings)


def get_embedder() -> EmbedderClient:
    """FastAPI 의존성 — 테스트에서 dependency_overrides로 가짜를 넣는다."""
    return EmbedderClient()
