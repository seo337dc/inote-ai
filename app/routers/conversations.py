from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException

from app.db.postgres import get_connection

router = APIRouter(prefix="/conversations", tags=["conversations"])

logger = logging.getLogger(__name__)


def upsert_session(
    session_id: str,
    user_id: str,
    post_id: Optional[str] = None,
    post_title: Optional[str] = None,
    post_category: Optional[str] = None,
) -> None:
    """세션이 없으면 만들고, 있으면 제목/카테고리 스냅샷과 updated_at을 갱신."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "insert into conversation_sessions "
                    "(id, user_id, post_id, post_title, post_category) "
                    "values (%s, %s, %s, %s, %s) "
                    "on conflict (id) do update set "
                    "post_title = coalesce(excluded.post_title, conversation_sessions.post_title), "
                    "post_category = coalesce(excluded.post_category, conversation_sessions.post_category), "
                    "updated_at = now()",
                    (session_id, user_id, post_id, post_title, post_category),
                )
    except Exception:
        logger.exception("failed to upsert session (session_id=%s)", session_id)


def save_message(session_id: str, role: str, content: str) -> None:
    """chat 라우터에서 fire-and-forget으로 대화를 저장할 때 쓰는 내부 헬퍼.

    저장 실패가 채팅 응답 자체를 막으면 안 되므로 예외를 삼킨다.
    """
    if not content:
        return
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "insert into conversations (session_id, role, content) values (%s, %s, %s)",
                    (session_id, role, content),
                )
    except Exception:
        logger.exception("failed to save message (session_id=%s, role=%s)", session_id, role)


@router.get("/{session_id}")
async def get_history(session_id: str, user_id: str):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "select user_id from conversation_sessions where id = %s", (session_id,)
            )
            row = cur.fetchone()
            if not row:
                return []  # 아직 메시지를 한 번도 안 보낸 세션 — 빈 이력이 맞음
            if row[0] != user_id:
                raise HTTPException(status_code=403, detail="본인 세션의 대화만 조회할 수 있습니다.")

            cur.execute(
                "select role, content from conversations "
                "where session_id = %s order by created_at asc",
                (session_id,),
            )
            rows = cur.fetchall()
    return [{"role": role, "content": content} for role, content in rows]
