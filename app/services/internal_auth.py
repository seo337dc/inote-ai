"""서비스 간 호출(내부 전용 엔드포인트)용 비밀키 검사 — 새 엔드포인트가 쓰는 FastAPI 의존성.

기존 라우터(summarize 등)는 `x-internal-secret != INTERNAL_SECRET`을 직접 비교한다. 여기서는 두 가지를 더 지킨다.
- 서버에 INTERNAL_SECRET이 설정돼 있지 않으면 **모든 요청을 거절**한다(열린 채로 뜨지 않게).
- 시간 차이로 비밀키를 추측하지 못하게 상수 시간 비교(secrets.compare_digest)를 쓴다.
"""

from __future__ import annotations

import os
import secrets

from fastapi import Header, HTTPException


def require_internal_secret(x_internal_secret: str = Header(default="")) -> None:
    expected = os.environ.get("INTERNAL_SECRET", "")
    if not expected:
        raise HTTPException(status_code=503, detail="INTERNAL_SECRET이 설정되지 않았습니다.")
    if not x_internal_secret or not secrets.compare_digest(x_internal_secret, expected):
        raise HTTPException(status_code=401, detail="unauthorized")
