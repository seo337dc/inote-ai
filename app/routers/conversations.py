from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from app.db.postgres import get_connection
from app.services.ownership import is_owner

router = APIRouter(prefix="/conversations", tags=["conversations"])

logger = logging.getLogger(__name__)


def save_message(post_id: str, user_id: str, role: str, content: str) -> None:
    """chat 라우터에서 fire-and-forget으로 대화를 저장할 때 쓰는 내부 헬퍼.

    저장 실패가 채팅 응답 자체를 막으면 안 되므로 예외를 삼킨다.
    """
    if not content:
        return
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "insert into conversations (post_id, user_id, role, content) "
                    "values (%s, %s, %s, %s)",
                    (post_id, user_id, role, content),
                )
    except Exception:
        logger.exception("failed to save message (post_id=%s, role=%s)", post_id, role)


@router.get("/{post_id}")
async def get_history(post_id: str, user_id: str):
    if not await is_owner(post_id, user_id):
        raise HTTPException(status_code=403, detail="본인 글의 대화만 조회할 수 있습니다.")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "select role, content from conversations "
                "where post_id = %s order by created_at asc",
                (post_id,),
            )
            rows = cur.fetchall()
    return [{"role": role, "content": content} for role, content in rows]
