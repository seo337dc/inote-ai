import os
import platform
from urllib.parse import urlsplit

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

from app.logging_config import register_error_handlers, setup_logging
from app.routers import book_info, chat, conversations, embeddings, reading_chat, sessions, summarize

setup_logging()

app = FastAPI(title="inote-ai")
register_error_handlers(app)

allowed_origins = os.environ.get("ALLOWED_ORIGINS", "http://localhost:3011").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat.router)
app.include_router(conversations.router)
app.include_router(sessions.router)
app.include_router(summarize.router)
app.include_router(book_info.router)
app.include_router(reading_chat.router)
app.include_router(embeddings.router)


@app.get("/health")
async def health():
    return {"status": "ok"}


def _describe_database_url(url: str) -> dict:
    """DATABASE_URL을 비밀번호 없이 요약 — 잘못 붙여 넣었는지 확인하는 용도."""
    if not url:
        return {"set": False}
    parts = urlsplit(url.strip().strip("\"'"))
    return {
        "set": True,
        "scheme": parts.scheme,
        "host": parts.hostname,
        "db": parts.path.lstrip("/"),
        "query_keys": sorted(q.split("=")[0] for q in parts.query.split("&") if q),
        "has_quote_or_space": any(c in url for c in " \"'\n\t"),
        "password_length": len(parts.password or ""),
    }


@app.get("/health/db")
async def health_db(x_internal_secret: str = Header(default="")):
    """DB 접속 진단 (internal 전용). 접속 성공 여부와, 실패하면 에러 종류·메시지를 돌려준다.
    비밀번호는 응답에 넣지 않는다 — DATABASE_URL은 호스트·DB 이름 등 구조만 요약해서 보여줌."""
    if x_internal_secret != os.environ.get("INTERNAL_SECRET"):
        raise HTTPException(status_code=401, detail="unauthorized")

    import psycopg

    from app.db.postgres import get_connection

    url = os.environ.get("DATABASE_URL", "")
    info = {
        "database_url": _describe_database_url(url),
        "python": platform.python_version(),
        "psycopg": psycopg.__version__,
        "libpq": psycopg.pq.version(),
    }
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("select 1")
                cur.fetchone()
                # 접속만 되고 테이블이 없는 경우(엉뚱한 DB를 가리킬 때)도 잡는다
                cur.execute(
                    "select table_name from information_schema.tables "
                    "where table_schema = 'public' "
                    "and table_name in ('conversation_sessions', 'conversations')"
                )
                found = {row[0] for row in cur.fetchall()}
        missing = sorted({"conversation_sessions", "conversations"} - found)
        if missing:
            return JSONResponse(
                status_code=503,
                content={"db": "connected-but-missing-tables", "missing_tables": missing, **info},
            )
        return {"db": "ok", **info}
    except Exception as e:  # noqa: BLE001 — 진단용이라 모든 예외를 잡아서 알려준다
        password = urlsplit(url.strip()).password or ""
        detail = str(e).replace(password, "***") if password else str(e)
        return JSONResponse(
            status_code=503,
            content={"db": "error", "type": type(e).__name__, "detail": detail[:500], **info},
        )
