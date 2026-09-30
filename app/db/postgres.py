from __future__ import annotations

import os

import psycopg


def get_connection() -> psycopg.Connection:
    # 서버리스(Vercel)에서는 요청마다 연결을 새로 열고 `with` 블록이 끝나면 닫는다.
    # DATABASE_URL은 Neon의 pooled 주소(호스트에 `-pooler`)를 써야 연결이 몰려도 버틴다.
    # prepare_threshold=None: 풀러(PgBouncer) 뒤에서 서버 측 prepared statement가 꼬이지 않게 끈다.
    return psycopg.connect(
        os.environ["DATABASE_URL"],
        connect_timeout=10,
        prepare_threshold=None,
    )
