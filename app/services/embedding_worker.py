"""임베딩 작업 대기열 워커 — embedding_job의 대기 작업을 한 건씩 가져와 임베딩하고 벡터를 저장한다.

흐름 (한 건): claim_next(processing, 시도 +1) → 같은 내용·모델의 벡터가 이미 있으면 임베딩 생략 / 없으면 임베딩 서비스 호출
            → complete(벡터 저장 + done을 한 트랜잭션으로). 끝난 작업의 본문은 비운다.

실패를 다루는 규칙 (워커가 가장 헷갈리기 쉬운 부분)
- **서비스에 닿지 못함**(주소 없음·연결 실패·시간 초과): 이 글의 잘못이 아니다 → 시도 횟수를 세지 않고(release) 그 자리에서 멈춘다.
  서비스가 꺼져 있다고 모든 작업이 failed가 되면 안 된다.
- **응답은 왔지만 실패**(5xx·이상한 응답·예상 못 한 오류): 글 때문일 수 있다 → 시도 횟수를 세고 pending으로 되돌린다.
  상한(max_attempts)에 닿으면 failed — 한 작업이 큐 앞을 막고 있지 않게 한다. 연속으로 여러 건 실패하면 서비스 문제로 보고 멈춘다.
- **임베딩할 내용이 없음**: 다시 해도 소용없으므로 바로 failed.
- **처리 중에 글이 다시 저장·삭제됨**: 오래된 내용으로 벡터를 쓰지 않고 이번 결과는 버린다(version 확인, complete 참고).
- 워커가 처리 도중 죽으면 작업이 processing으로 남는다 → stale_seconds가 지나면 다른 실행이 다시 가져간다.

로그에는 글 id와 결과만 남긴다(제목·본문은 남기지 않는다 — 비공개 글일 수 있다).
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Optional

from app.services.embedder_client import EmbedderClient, EmbedderUnavailable, expected_model
from app.services.embedding_jobs import EmbeddingJobStore, Job
from app.services.embedding_store import EmbeddingStore, upsert_with_cursor
from app.services.text_prep import build_embedding_text, content_hash, html_to_text

logger = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_STALE_SECONDS = 600.0  # 처리 중으로 이만큼 갱신이 없으면 워커가 멈춘 것으로 본다(글 한 편 임베딩은 수 초~수십 초)
DEFAULT_MAX_CONSECUTIVE_FAILURES = 3


def _env_number(name: str, default, cast, low, high):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        return default
    return value if low <= value <= high else default


@dataclass(frozen=True)
class WorkerConfig:
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    stale_seconds: float = DEFAULT_STALE_SECONDS
    max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES

    @classmethod
    def from_env(cls) -> "WorkerConfig":
        """EMBEDDING_JOB_MAX_ATTEMPTS(1~20), EMBEDDING_JOB_STALE_SECONDS(10~86400). 없거나 범위를 벗어나면 기본값."""
        return cls(
            max_attempts=_env_number("EMBEDDING_JOB_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS, int, 1, 20),
            stale_seconds=_env_number("EMBEDDING_JOB_STALE_SECONDS", DEFAULT_STALE_SECONDS, float, 10.0, 86400.0),
        )


@dataclass
class RunSummary:
    done: int = 0  # 임베딩해서 벡터를 저장하고 완료
    unchanged: int = 0  # 같은 내용·모델의 벡터가 이미 있어 임베딩 없이 완료
    superseded: int = 0  # 처리 중에 글이 다시 저장되거나 삭제돼 이번 결과를 버림
    retried: int = 0  # 실패했지만 시도 횟수가 남아 재시도 대기
    failed: int = 0  # 영구 실패(상한 도달 포함)
    abandoned: int = 0  # 워커가 멈춘 채 상한에 닿아 failed로 표시된 작업
    stopped: Optional[str] = None  # 중간에 멈춘 이유(없으면 큐를 다 비웠다)

    @property
    def handled(self) -> int:
        return self.done + self.unchanged + self.superseded + self.retried + self.failed

    @property
    def has_problem(self) -> bool:
        return self.failed > 0 or self.abandoned > 0 or self.stopped is not None


class EmbeddingWorker:
    def __init__(
        self,
        jobs: EmbeddingJobStore,
        store: EmbeddingStore,
        embedder: EmbedderClient,
        config: Optional[WorkerConfig] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> None:
        self._jobs, self._store, self._embedder = jobs, store, embedder
        self._config = config or WorkerConfig()
        self.stop_event = stop_event or threading.Event()  # 세우면 지금 작업까지만 끝내고 멈춘다(Ctrl+C 처리용)

    def run_once(self) -> RunSummary:
        """대기 작업을 가능한 만큼 처리하고 끝낸다(큐가 비었거나, 서비스 문제로 멈추거나, 중단 요청이 올 때까지)."""
        cfg = self._config
        summary = RunSummary()
        summary.abandoned = self._jobs.fail_abandoned(cfg.max_attempts, cfg.stale_seconds)
        consecutive_failures = 0
        while True:
            if self.stop_event.is_set():
                summary.stopped = "중단 요청으로 멈춥니다."
                break
            job = self._jobs.claim_next(cfg.max_attempts, cfg.stale_seconds)
            if job is None:
                break
            outcome = self._handle(job, summary)
            if outcome == "stop":
                break
            consecutive_failures = consecutive_failures + 1 if outcome == "failed" else 0
            if consecutive_failures >= cfg.max_consecutive_failures:
                summary.stopped = f"연속 {consecutive_failures}건 실패해서 멈춥니다(임베딩 서비스 문제일 수 있습니다)."
                break
        return summary

    def run_loop(self, interval: float, on_summary=None) -> None:
        """interval초마다 run_once를 반복한다. stop_event가 서면 끝난다. 서비스가 꺼져 있어도 멈추지 않고 다음 주기에 다시 시도한다."""
        while not self.stop_event.is_set():
            summary = self.run_once()
            if on_summary is not None:
                on_summary(summary)
            self.stop_event.wait(interval)

    # --- 한 건 처리: "ok"(정상 또는 글 때문이 아닌 결과) / "failed"(실패 기록) / "stop"(서비스에 닿지 못해 멈춤) ---
    def _handle(self, job: Job, summary: RunSummary) -> str:
        cfg = self._config
        text = build_embedding_text(job.title, html_to_text(job.content or ""))
        if not text:
            self._record_failure(job, summary, "임베딩할 내용이 없습니다.", permanent=True)
            return "ok"  # 글 내용의 문제라 서비스 장애 판단(연속 실패)에는 넣지 않는다
        digest = content_hash(text)
        try:
            if self._store.get_state(job.post_id) == (expected_model(), digest):
                applied = self._jobs.complete(job.post_id, job.version, lambda cur: None)
                if applied:
                    summary.unchanged += 1
                    logger.info("post %s: 이미 최신 벡터가 있어 완료 처리", job.post_id)
                else:
                    summary.superseded += 1
                return "ok"

            result = self._embedder.embed([text])
            applied = self._jobs.complete(
                job.post_id,
                job.version,
                lambda cur: upsert_with_cursor(cur, job.post_id, result.model, digest, result.embeddings[0]),
            )
        except EmbedderUnavailable as e:
            if e.unreachable:
                self._jobs.release(job.post_id, job.version, f"임베딩 서비스에 닿지 못함: {e}")
                summary.stopped = "임베딩 서비스에 연결할 수 없어 멈춥니다(작업은 대기 상태로 남아 있습니다)."
                logger.warning("post %s: 임베딩 서비스에 닿지 못해 되돌림", job.post_id)
                return "stop"
            self._record_failure(job, summary, f"임베딩 서비스 오류: {e}")
            return "failed"
        except Exception as e:  # DB·예상 못 한 오류 — 메시지에 값이 섞일 수 있어 종류만 기록한다
            self._record_failure(job, summary, f"{type(e).__name__}")
            return "failed"

        if applied:
            summary.done += 1
            logger.info("post %s: 임베딩 완료 (시도 %s회째)", job.post_id, job.attempts)
        else:
            summary.superseded += 1
            logger.info("post %s: 처리 중에 글이 바뀌어(또는 삭제돼) 이번 결과는 버림", job.post_id)
        return "ok"

    def _record_failure(self, job: Job, summary: RunSummary, error: str, permanent: bool = False) -> None:
        status = self._jobs.fail(job.post_id, job.version, error, self._config.max_attempts, permanent=permanent)
        if status == "failed":
            summary.failed += 1
            logger.warning("post %s: 실패로 표시 (%s)", job.post_id, error)
        elif status == "pending":
            summary.retried += 1
            logger.warning("post %s: 실패, 재시도 대기 (%s회 시도) — %s", job.post_id, job.attempts, error)
        else:
            summary.superseded += 1  # 그 사이 글이 다시 저장돼 이 실패 기록은 필요 없다
