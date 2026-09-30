from __future__ import annotations

import logging
import sys

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("inote-ai")


def request_id_of(request: Request) -> str:
    """Vercel이 요청마다 붙이는 x-vercel-id (로그에서 이 요청을 찾는 열쇠). 없으면 '-'."""
    return request.headers.get("x-vercel-id") or request.headers.get("x-request-id") or "-"


def setup_logging() -> None:
    """에러 로그를 표준 출력으로 남긴다 (Vercel 로그에서 확인). 요청 본문·헤더·키는 남기지 않는다."""
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )
    # httpx는 INFO에 요청 URL 전체(검색어·글 id 포함)를 찍으므로 경고 이상만 남긴다
    logging.getLogger("httpx").setLevel(logging.WARNING)


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(Exception)
    async def unhandled_exception(request: Request, exc: Exception):
        rid = request_id_of(request)
        logger.error(
            "unhandled exception method=%s path=%s request_id=%s",
            request.method,
            request.url.path,
            rid,
            exc_info=exc,
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal Server Error", "request_id": rid},
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception(request: Request, exc: StarletteHTTPException):
        # 의도적으로 던진 5xx(예: 빈 요약 502)도 원인을 추적할 수 있게 남긴다. 4xx는 정상 흐름이라 남기지 않음.
        if exc.status_code >= 500:
            logger.error(
                "http %s method=%s path=%s request_id=%s detail=%s",
                exc.status_code,
                request.method,
                request.url.path,
                request_id_of(request),
                exc.detail,
            )
        return await http_exception_handler(request, exc)
