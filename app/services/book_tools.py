from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

KAKAO_BOOK_SEARCH_URL = "https://dapi.kakao.com/v3/search/book"

# Groq(OpenAI 호환) function-calling에 그대로 넘기는 도구 스펙.
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_books",
            "description": (
                "특정 도서를 제목/저자로 검색. 책 가격, 출판사, 검색 결과, "
                "실존 여부 확인이 필요할 때 사용."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "검색어 (책 제목 또는 저자명)",
                    },
                    "size": {
                        "type": "integer",
                        "description": "결과 개수 (기본 5, 최대 10)",
                    },
                },
                "required": ["query"],
            },
        },
    },
]


async def search_books(query: str, size: int = 5) -> list[dict]:
    """카카오 도서 검색 API 호출 — 제목/저자/출판사/정가/할인가를 반환.
    실패해도 예외를 던지지 않고 빈 목록을 반환 — LLM이 "검색 결과 없음"으로 자연스럽게 처리함."""
    api_key = os.environ.get("KAKAO_REST_API_KEY", "")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.get(
                KAKAO_BOOK_SEARCH_URL,
                headers={"Authorization": f"KakaoAK {api_key}"},
                params={"query": query, "size": min(max(size, 1), 10)},
            )
        if res.status_code != 200:
            logger.warning("kakao book search failed: %s %s", res.status_code, res.text)
            return []

        docs = res.json().get("documents", [])
        return [
            {
                "title": d.get("title"),
                "authors": d.get("authors"),
                "publisher": d.get("publisher"),
                "price": d.get("price"),
                "sale_price": d.get("sale_price"),
                "url": d.get("url"),
            }
            for d in docs
        ]
    except Exception:
        logger.exception("kakao book search error (query=%s)", query)
        return []
