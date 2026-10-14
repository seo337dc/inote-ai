from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, Header, HTTPException

from app.db.postgres import get_connection

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.get("")
async def list_sessions(user_id: str, q: Optional[str] = None):
    """세션 목록. q가 있으면 글 제목이나 대화 내용에 그 문구가 포함된 세션만."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            if q:
                like = f"%{q}%"
                cur.execute(
                    "select distinct s.id, s.post_id, s.post_title, s.post_category, "
                    "s.updated_at, "
                    "(select content from conversations c2 where c2.session_id = s.id "
                    " order by c2.created_at desc limit 1) as last_message, s.kind, "
                    "(select content from conversations c3 where c3.session_id = s.id and c3.role = 'user' "
                    " order by c3.created_at asc limit 1) as first_question "
                    "from conversation_sessions s "
                    "left join conversations c on c.session_id = s.id "
                    "where s.user_id = %s "
                    "and (s.post_title ilike %s or c.content ilike %s) "
                    "order by s.updated_at desc",
                    (user_id, like, like),
                )
            else:
                cur.execute(
                    "select s.id, s.post_id, s.post_title, s.post_category, s.updated_at, "
                    "(select content from conversations c where c.session_id = s.id "
                    " order by c.created_at desc limit 1) as last_message, s.kind, "
                    "(select content from conversations c3 where c3.session_id = s.id and c3.role = 'user' "
                    " order by c3.created_at asc limit 1) as first_question "
                    "from conversation_sessions s "
                    "where s.user_id = %s "
                    "order by s.updated_at desc",
                    (user_id,),
                )
            rows = cur.fetchall()

    return [
        {
            "id": r[0],
            "post_id": r[1],
            "post_title": r[2],
            "post_category": r[3],
            "updated_at": r[4].isoformat(),
            "last_message": r[5],
            "kind": r[6],
            "first_question": r[7],  # 세션의 첫 사용자 질문 — 목록 제목으로 쓴다(마지막 말은 긴 답변일 수 있다)
        }
        for r in rows
    ]


@router.delete("")
async def delete_user_sessions(user_id: str, x_internal_secret: str = Header(default="")):
    """회원 탈퇴 시 inote-server가 호출 — 그 유저의 세션·대화 기록을 전부 삭제.
    internal 전용(x-internal-secret). conversations는 session_id FK가 cascade라 세션만 지우면 됨."""
    if x_internal_secret != os.environ.get("INTERNAL_SECRET"):
        raise HTTPException(status_code=401, detail="unauthorized")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("delete from conversation_sessions where user_id = %s", (user_id,))
            deleted = cur.rowcount

    return {"deleted_sessions": deleted}


@router.delete("/{session_id}")
async def delete_session(session_id: str, x_internal_secret: str = Header(default="")):
    """글(post) 삭제 시 inote-server가 호출 — 그 글에 묶인 세션 하나만 삭제.
    글쓰기 세션의 id는 post_id를 그대로 쓰므로 session_id에 postId를 넘기면 됨.
    internal 전용(x-internal-secret). conversations는 cascade라 세션만 지우면 됨."""
    if x_internal_secret != os.environ.get("INTERNAL_SECRET"):
        raise HTTPException(status_code=401, detail="unauthorized")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("delete from conversation_sessions where id = %s", (session_id,))
            deleted = cur.rowcount

    return {"deleted_sessions": deleted}
