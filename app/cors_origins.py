"""CORS 허용 출처(ALLOWED_ORIGINS) 해석.

환경변수는 사람이 손으로 입력하는 값이라 `https://a.app, http://localhost:3011`처럼 쉼표 뒤에 공백을 넣기 쉽다.
그대로 쉼표로만 자르면 두 번째 출처가 `" http://localhost:3011"`(앞에 공백)이 되어, 브라우저가 보내는
`http://localhost:3011`과 글자가 달라 **조용히 거절**된다(첫 항목은 공백이 없어 통과해서 알아채기 어렵다).
그래서 항목마다 다음을 정리한다 — 앞뒤 공백, 값 전체를 감싼 따옴표, 끝의 `/`(브라우저의 출처에는 경로가 없다) — 그리고
빈 항목과 중복은 버린다.
"""

from __future__ import annotations

from typing import List, Optional

DEFAULT_ORIGINS = "http://localhost:3011"


def _clean(item: str) -> str:
    value = item.strip()
    # 값 전체가 따옴표로 감싸져 들어온 경우(Vercel 등에 따옴표째 붙여 넣은 경우)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value.rstrip("/")


def parse_allowed_origins(raw: Optional[str]) -> List[str]:
    """ALLOWED_ORIGINS 값을 허용 출처 목록으로 바꾼다.

    - 환경변수가 없으면(None) 로컬 개발용 기본값만 허용한다.
    - 있는데 쓸 수 있는 항목이 하나도 없으면(빈 값, 쉼표·공백뿐) 빈 목록 — 아무 출처도 허용하지 않는다(설정 실수를 열어 두지 않는다).
    """
    if raw is None:
        raw = DEFAULT_ORIGINS
    origins: List[str] = []
    for item in raw.split(","):
        origin = _clean(item)
        if origin and origin not in origins:
            origins.append(origin)
    return origins
