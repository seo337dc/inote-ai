from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)


async def is_owner(post_id: str, user_id: str) -> bool:
    """inote-server에 그 글의 작성자 ID를 물어봐서 요청자와 일치하는지 확인.

    inote-server가 응답을 못 하거나 에러를 내면, 안전하게 "작성자 아님"으로 처리한다
    (대화 기록은 실패를 열어두는 것보다 닫아두는 쪽이 안전).
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.get(
                f"{os.environ['INOTE_SERVER_URL']}/blog/posts/{post_id}/owner",
                headers={"x-internal-secret": os.environ.get("INTERNAL_SECRET", "")},
            )
        if res.status_code != 200:
            return False
        return res.json().get("userId") == user_id
    except Exception:
        logger.exception("failed to verify post ownership (post_id=%s)", post_id)
        return False
