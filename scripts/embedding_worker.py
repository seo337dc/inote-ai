"""임베딩 작업 대기열 워커 실행 — 대기 작업(embedding_job)을 한꺼번에 처리한다. (Vercel 번들에는 들어가지 않는다: .vercelignore)

운영(Vercel)은 임베딩 서비스에 닿지 못해 글 저장 시 작업을 대기열에 남긴다. 이 스크립트를 임베딩 서비스가 있는 곳(내 PC)에서 돌려 처리한다.

  # 개발 DB + 로컬 임베딩 서비스 — 대기 작업을 모두 처리하고 끝낸다 (레포 루트에서)
  DATABASE_URL="postgresql://ai:<AI_DEV_DB_PASSWORD>@127.0.0.1:5434/inote_ai_dev" \\
  EMBEDDER_URL="http://127.0.0.1:8100" EMBEDDER_SECRET="<임베딩 서비스 INTERNAL_SECRET>" \\
  venv/bin/python -m scripts.embedding_worker

옵션
  --loop [--interval 30]  다 처리해도 끝내지 않고 interval초마다 다시 확인한다 (Ctrl+C: 지금 작업까지 끝내고 종료)
  --status                대기열 상태(상태별 개수)만 보고 끝낸다
  --retry-failed          failed 작업을 처음부터 다시 시도하도록 되돌린 뒤 실행한다 (원인을 고친 뒤)
  --max-attempts N        작업 하나의 최대 시도 횟수 (기본 5, 환경변수 EMBEDDING_JOB_MAX_ATTEMPTS)
  --allow-remote          개발 DB가 아닌 DB(운영 등)에서 실행할 때만 붙인다

안전장치
- **.env를 읽지 않는다.** DATABASE_URL을 환경변수로 직접 줘야 한다(.env의 DATABASE_URL은 운영 DB라서, 읽으면 실수로 운영에 쓰게 된다).
- 대상 DB가 로컬(127.0.0.1/localhost)이고 DB 이름이 `_dev`로 끝나는 개발 DB가 아니면 --allow-remote가 없는 한 실행하지 않는다.
- 실행 첫 줄에 `대상: DB … → 임베딩 서비스 …`를 출력한다(비밀번호는 출력하지 않는다). 확인하고 쓴다.

종료 코드: 0 문제 없음 / 1 실패한 작업이 있거나 중간에 멈춤 / 2 실행하지 못함(설정 오류·대상 거절)
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import signal
import sys
from typing import List, Optional
from urllib.parse import urlparse

from app.services.embedder_client import EmbedderClient, configured_retries, configured_timeout
from app.services.embedding_jobs import EmbeddingJobStore
from app.services.embedding_store import EmbeddingStore
from app.services.embedding_worker import EmbeddingWorker, RunSummary, WorkerConfig

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class TargetNotAllowed(Exception):
    """대상 DB가 개발 DB가 아닌데 --allow-remote가 없다."""


def check_target(database_url: str, allow_remote: bool) -> str:
    """실행해도 되는 대상인지 확인하고, 출력용 'host:port/dbname'(비밀번호 없음)을 돌려준다."""
    try:
        parsed = urlparse(database_url)
        host, port, dbname = parsed.hostname or "", parsed.port, parsed.path.lstrip("/")
    except ValueError as e:
        raise TargetNotAllowed("DATABASE_URL을 해석할 수 없습니다.") from e
    if not host or not dbname:
        raise TargetNotAllowed("DATABASE_URL에 호스트와 DB 이름이 있어야 합니다.")
    label = f"{host}:{port}/{dbname}" if port else f"{host}/{dbname}"
    is_local_dev = host in LOCAL_HOSTS and dbname.endswith("_dev")
    if not is_local_dev and not allow_remote:
        raise TargetNotAllowed(
            f"{label}는 개발 DB(로컬 호스트 + 이름이 _dev로 끝남)가 아닙니다. 정말 이 DB에서 실행하려면 --allow-remote를 붙이세요."
        )
    return label


def format_summary(summary: RunSummary) -> str:
    parts = [
        f"처리 {summary.handled}건",
        f"완료 {summary.done}",
        f"이미 최신 {summary.unchanged}",
        f"버림(글이 바뀜) {summary.superseded}",
        f"재시도 대기 {summary.retried}",
        f"실패 {summary.failed}",
    ]
    if summary.abandoned:
        parts.append(f"멈춘 워커의 작업을 실패 처리 {summary.abandoned}")
    line = " · ".join(parts)
    return f"{line}\n⚠️ {summary.stopped}" if summary.stopped else line


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="임베딩 작업 대기열 워커 (로컬 전용)")
    p.add_argument("--loop", action="store_true", help="다 처리해도 끝내지 않고 주기적으로 다시 확인")
    p.add_argument("--interval", type=float, default=30.0, help="--loop의 확인 주기(초, 기본 30)")
    p.add_argument("--status", action="store_true", help="대기열 상태만 보고 끝낸다")
    p.add_argument("--retry-failed", action="store_true", help="failed 작업을 다시 시도하도록 되돌린 뒤 실행")
    p.add_argument("--max-attempts", type=int, default=None, help="작업 하나의 최대 시도 횟수")
    p.add_argument("--allow-remote", action="store_true", help="개발 DB가 아닌 DB에서 실행할 때만")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("DATABASE_URL이 없습니다. 환경변수로 직접 지정하세요(.env는 읽지 않습니다).", file=sys.stderr)
        return 2
    try:
        target = check_target(database_url, args.allow_remote)
    except TargetNotAllowed as e:
        print(f"실행하지 않습니다: {e}", file=sys.stderr)
        return 2
    embedder_url = os.environ.get("EMBEDDER_URL", "").strip()
    print(f"대상: DB {target} → 임베딩 서비스 {embedder_url or '(EMBEDDER_URL 없음)'}")

    jobs = EmbeddingJobStore()
    if args.status:
        counts = jobs.counts()
        print("대기열 상태: " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "비어 있음"))
        return 0
    if args.retry_failed:
        print(f"failed 작업 {jobs.requeue_failed()}건을 다시 시도하도록 되돌렸습니다.")

    config = WorkerConfig.from_env()
    if args.max_attempts is not None:
        config = dataclasses.replace(config, max_attempts=max(1, args.max_attempts))
    embedder = EmbedderClient(timeout=configured_timeout(), retries=configured_retries())
    worker = EmbeddingWorker(jobs, EmbeddingStore(), embedder, config)

    def request_stop(signum, frame):
        print("\n중단 요청을 받았습니다. 지금 작업까지 끝내고 멈춥니다. (한 번 더 누르면 즉시 종료)")
        worker.stop_event.set()
        signal.signal(signum, signal.SIG_DFL)  # 두 번째 신호는 기본 동작(즉시 종료)

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    if args.loop:
        print(f"{args.interval:g}초마다 대기열을 확인합니다. (Ctrl+C로 종료)")
        worker.run_loop(args.interval, on_summary=lambda s: print(format_summary(s)) if (s.handled or s.stopped or s.abandoned) else None)
        return 0

    summary = worker.run_once()
    print(format_summary(summary))
    return 1 if summary.has_problem else 0


if __name__ == "__main__":
    sys.exit(main())
