"""embedding_job 테이블(AI DB) 접근 — 임베딩 서비스에 닿지 못한 저장 요청을 남겨 두는 대기열.

글 하나에 행 하나(post_id가 키)라서, 같은 글을 다시 저장하면 새 행이 아니라 그 행을 갱신한다(중복 임베딩 방지).
⚠️ post_embedding과 달리 **글 본문(HTML)을 잠시 저장한다**(비공개 글 포함). 처리가 끝나면 비우고, 글이 삭제되면 지운다.
(failed 작업은 재시도할 수 있게 본문을 남긴다 — 글을 삭제하면 함께 지워진다.)

상태 흐름
  pending ──claim_next──▶ processing ──complete──▶ done (본문 비움)
     ▲                        │ └──fail(시도 횟수 < 상한)──▶ pending
     │                        └────fail(상한 도달·영구 실패)──▶ failed
     └── release(서비스에 닿지 못함: 시도 횟수를 되돌리고 대기) / requeue_failed / enqueue(글을 다시 저장)

처리 중에 글이 다시 저장되면 version이 올라간다. 워커는 가져올 때의 version을 들고 있다가, 끝날 때 같을 때만 반영한다(오래된 내용 덮어쓰기 방지).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Optional, Sequence

from app.db.postgres import get_connection

MAX_ERROR_CHARS = 500  # last_error에 저장하는 길이 — 글 내용이 아니라 오류 종류·요약만 남긴다


@dataclass(frozen=True)
class Job:
    post_id: str
    title: str
    content: Optional[str]  # HTML 본문. 정상 작업에는 있다
    version: int
    attempts: int  # 이번에 가져온 것까지 센 시도 횟수


@dataclass(frozen=True)
class JobStatus:
    status: str  # pending | processing | done | failed
    attempts: int
    last_error: Optional[str]


class EmbeddingJobStore:
    def enqueue(self, post_id: str, title: str, content: str) -> int:
        """작업을 등록한다. 같은 글의 작업이 이미 있으면(어떤 상태든) 내용을 갱신하고 pending으로 되돌린다.
        갱신된 version을 돌려준다 — 처리 중에 내용이 바뀌었는지 확인하는 기준이다."""
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                insert into embedding_job (post_id, title, content)
                values (%s, %s, %s)
                on conflict (post_id) do update set
                  title = excluded.title, content = excluded.content,
                  status = 'pending', attempts = 0, last_error = null,
                  version = embedding_job.version + 1, updated_at = now()
                returning version
                """,
                (post_id, title, content),
            )
            return int(cur.fetchone()[0])

    def delete(self, post_id: str) -> bool:
        """지웠으면 True, 원래 없었으면 False (어느 쪽이든 오류가 아니다)."""
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("delete from embedding_job where post_id = %s", (post_id,))
            return cur.rowcount > 0

    # --- 워커용 ---
    def claim_next(self, max_attempts: int, stale_seconds: float) -> Optional[Job]:
        """처리할 작업 하나를 가져와 processing으로 바꾼다(시도 횟수 +1). 없으면 None.

        - 오래된 순(FIFO). 이미 상한까지 시도한 작업은 가져오지 않는다.
        - **FOR UPDATE SKIP LOCKED**: 워커가 여러 개여도 같은 작업을 동시에 가져가지 않는다(다른 워커가 잡은 행은 건너뛴다).
        - processing인 채 stale_seconds보다 오래 갱신되지 않은 작업은 워커가 중간에 멈춘 것으로 보고 다시 가져간다.
        하나의 SQL 문이라 '고르기'와 '표시하기' 사이에 다른 워커가 끼어들 수 없다."""
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                with next as (
                  select post_id from embedding_job
                  where attempts < %s
                    and (status = 'pending'
                         or (status = 'processing' and updated_at < now() - make_interval(secs => %s)))
                  order by updated_at
                  limit 1
                  for update skip locked
                )
                update embedding_job j
                   set status = 'processing', attempts = j.attempts + 1, updated_at = now()
                  from next
                 where j.post_id = next.post_id
                returning j.post_id, j.title, j.content, j.version, j.attempts
                """,
                (max_attempts, float(stale_seconds)),
            )
            row = cur.fetchone()
        return Job(post_id=row[0], title=row[1], content=row[2], version=int(row[3]), attempts=int(row[4])) if row else None

    def complete(self, post_id: str, version: int, write_vector: Callable) -> bool:
        """벡터 저장과 '완료 처리'를 **한 트랜잭션**으로 한다. 반영했으면 True.

        행을 잠근 채 version과 상태를 확인한다. 그 사이 글이 삭제됐거나 다시 저장돼 version이 달라졌으면
        오래된 내용의 벡터를 쓰지 않고 False(이번 결과는 버린다 — 새 내용의 작업이 대기 중이거나 이미 처리됐다).
        write_vector(cur)는 같은 트랜잭션의 cursor로 벡터를 저장하는 함수다(예: upsert_with_cursor)."""
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("select version, status from embedding_job where post_id = %s for update", (post_id,))
            row = cur.fetchone()
            if row is None or int(row[0]) != version or row[1] != "processing":
                return False
            write_vector(cur)
            cur.execute(
                "update embedding_job set status = 'done', content = null, last_error = null, updated_at = now() where post_id = %s",
                (post_id,),
            )
            return True

    def fail(self, post_id: str, version: int, error: str, max_attempts: int, permanent: bool = False) -> Optional[str]:
        """처리 실패를 기록한다. 시도 횟수가 상한에 닿았거나 permanent(다시 해도 소용없는 실패)면 failed, 아니면 pending(재시도 대기).
        돌려주는 값은 바뀐 상태. version이 달라졌거나(그 사이 다시 저장됨) processing이 아니면 건드리지 않고 None."""
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                update embedding_job
                   set status = case when %s or attempts >= %s then 'failed' else 'pending' end,
                       last_error = %s, updated_at = now()
                 where post_id = %s and version = %s and status = 'processing'
                returning status
                """,
                (permanent, max_attempts, error[:MAX_ERROR_CHARS], post_id, version),
            )
            row = cur.fetchone()
        return row[0] if row else None

    def release(self, post_id: str, version: int, note: str) -> bool:
        """서비스에 닿지 못해 처리하지 못한 작업을 되돌린다 — 이 글의 잘못이 아니므로 시도 횟수를 세지 않는다(pending, attempts -1)."""
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                update embedding_job
                   set status = 'pending', attempts = greatest(attempts - 1, 0), last_error = %s, updated_at = now()
                 where post_id = %s and version = %s and status = 'processing'
                """,
                (note[:MAX_ERROR_CHARS], post_id, version),
            )
            return cur.rowcount > 0

    def fail_abandoned(self, max_attempts: int, stale_seconds: float) -> int:
        """워커가 중간에 멈춘 채 시도 횟수 상한에 닿은 작업은 더 가져갈 수 없으니 failed로 표시한다. 바꾼 개수."""
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                update embedding_job
                   set status = 'failed', last_error = '처리 중에 워커가 멈춘 것으로 보입니다(재시도 횟수 초과)', updated_at = now()
                 where status = 'processing' and attempts >= %s and updated_at < now() - make_interval(secs => %s)
                """,
                (max_attempts, float(stale_seconds)),
            )
            return cur.rowcount

    def requeue_failed(self) -> int:
        """failed 작업을 처음부터 다시 시도하도록 pending으로 되돌린다(원인을 고친 뒤). 바꾼 개수."""
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                update embedding_job
                   set status = 'pending', attempts = 0, last_error = null, updated_at = now()
                 where status = 'failed' and content is not null
                """
            )
            return cur.rowcount

    def counts(self) -> Dict[str, int]:
        """상태별 작업 수."""
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("select status, count(*) from embedding_job group by status")
            return {row[0]: int(row[1]) for row in cur.fetchall()}

    def statuses(self, post_ids: Sequence[str]) -> Dict[str, JobStatus]:
        """글별 작업 상태(본문 content는 읽지 않는다). 작업이 없는 글은 결과에 없다. 읽기 전용(관리자 상태 조회용)."""
        result: Dict[str, JobStatus] = {}
        if not post_ids:
            return result
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "select post_id, status, attempts, last_error from embedding_job where post_id = any(%s)",
                (list(post_ids),),
            )
            for post_id, status, attempts, last_error in cur.fetchall():
                result[post_id] = JobStatus(status=status, attempts=int(attempts), last_error=last_error)
        return result


def get_job_store() -> EmbeddingJobStore:
    """FastAPI 의존성 — 테스트에서 dependency_overrides로 가짜를 넣는다."""
    return EmbeddingJobStore()
