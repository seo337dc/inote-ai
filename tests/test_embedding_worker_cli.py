"""워커 실행 스크립트(scripts/embedding_worker.py)의 안전장치 테스트 — DB·네트워크 없음.
핵심: 개발 DB가 아니면 --allow-remote 없이는 실행하지 않고, .env를 읽지 않으며, 비밀번호를 출력하지 않는다."""

import contextlib
import io
import os
import unittest
from unittest import mock

from app.services.embedding_worker import RunSummary
from scripts import embedding_worker as cli

DEV_URL = "postgresql://ai:s3cret-pw@127.0.0.1:5434/inote_ai_dev"
PROD_LIKE_URL = "postgresql://postgres.abc:prod-pw@aws-0-ap.pooler.supabase.com:6543/postgres"


def run_main(argv, env, jobs_cls=None):
    """main을 실행해 (종료 코드, 표준 출력, 표준 오류)를 돌려준다. 환경변수는 env로만 구성한다."""
    out, err = io.StringIO(), io.StringIO()
    patches = [mock.patch.dict(os.environ, env, clear=True)]
    if jobs_cls is not None:
        patches.append(mock.patch.object(cli, "EmbeddingJobStore", jobs_cls))
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class CheckTargetTest(unittest.TestCase):
    def test_로컬_개발_DB는_허용하고_비밀번호_없는_라벨을_돌려준다(self):
        label = cli.check_target(DEV_URL, allow_remote=False)

        self.assertEqual(label, "127.0.0.1:5434/inote_ai_dev")
        self.assertNotIn("s3cret-pw", label)

    def test_localhost도_개발_DB로_본다(self):
        self.assertEqual(cli.check_target("postgresql://u:p@localhost:5434/x_dev", False), "localhost:5434/x_dev")

    def test_로컬이어도_이름이_dev로_끝나지_않으면_거절한다(self):
        with self.assertRaises(cli.TargetNotAllowed):
            cli.check_target("postgresql://u:p@127.0.0.1:5434/inote_ai", allow_remote=False)

    def test_원격_DB는_allow_remote가_없으면_거절하고_있으면_허용한다(self):
        with self.assertRaises(cli.TargetNotAllowed) as ctx:
            cli.check_target(PROD_LIKE_URL, allow_remote=False)
        self.assertIn("--allow-remote", str(ctx.exception))
        self.assertNotIn("prod-pw", str(ctx.exception))  # 거절 메시지에도 비밀번호가 없다

        self.assertEqual(cli.check_target(PROD_LIKE_URL, allow_remote=True), "aws-0-ap.pooler.supabase.com:6543/postgres")

    def test_호스트나_DB_이름이_없는_주소는_거절한다(self):
        for url in ("not-a-url", "postgresql://u:p@/dbname", "postgresql://u:p@127.0.0.1:5434/"):
            with self.subTest(url=url), self.assertRaises(cli.TargetNotAllowed):
                cli.check_target(url, allow_remote=True)


class MainTest(unittest.TestCase):
    def test_DATABASE_URL이_없으면_실행하지_않는다(self):
        jobs_cls = mock.Mock()

        code, _, err = run_main([], {}, jobs_cls)

        self.assertEqual(code, 2)
        self.assertIn("DATABASE_URL", err)
        jobs_cls.assert_not_called()  # DB를 건드리지 않았다

    def test_원격_DB를_allow_remote_없이_지정하면_DB를_건드리지_않고_거절한다(self):
        jobs_cls = mock.Mock()

        code, out, err = run_main([], {"DATABASE_URL": PROD_LIKE_URL}, jobs_cls)

        self.assertEqual(code, 2)
        self.assertIn("--allow-remote", err)
        jobs_cls.assert_not_called()
        self.assertNotIn("prod-pw", out + err)

    def test_첫_줄에_대상을_출력하고_비밀번호는_출력하지_않는다(self):
        jobs = mock.Mock()
        jobs.counts.return_value = {"pending": 2, "failed": 1}

        code, out, err = run_main(["--status"], {"DATABASE_URL": DEV_URL, "EMBEDDER_URL": "http://127.0.0.1:8100"}, mock.Mock(return_value=jobs))

        self.assertEqual(code, 0)
        self.assertTrue(out.splitlines()[0].startswith("대상: DB 127.0.0.1:5434/inote_ai_dev → 임베딩 서비스 http://127.0.0.1:8100"))
        self.assertIn("failed 1, pending 2", out)
        self.assertNotIn("s3cret-pw", out + err)

    def test_status는_대기열을_처리하지_않는다(self):
        jobs = mock.Mock()
        jobs.counts.return_value = {}

        with mock.patch.object(cli, "EmbeddingWorker") as worker_cls:
            code, out, _ = run_main(["--status"], {"DATABASE_URL": DEV_URL}, mock.Mock(return_value=jobs))

        worker_cls.assert_not_called()
        self.assertIn("비어 있음", out)

    def test_retry_failed는_되돌린_개수를_알리고_이어서_실행한다(self):
        jobs = mock.Mock()
        jobs.requeue_failed.return_value = 3
        worker = mock.Mock()
        worker.run_once.return_value = RunSummary(done=3)

        with mock.patch.object(cli, "EmbeddingWorker", return_value=worker):
            code, out, _ = run_main(["--retry-failed"], {"DATABASE_URL": DEV_URL}, mock.Mock(return_value=jobs))

        self.assertEqual(code, 0)
        self.assertIn("failed 작업 3건", out)
        worker.run_once.assert_called_once()

    def test_한_번_실행은_문제가_있으면_종료_코드_1(self):
        for summary, expected in ((RunSummary(done=2), 0), (RunSummary(done=1, failed=1), 1), (RunSummary(stopped="멈춤"), 1)):
            with self.subTest(summary=summary):
                worker = mock.Mock()
                worker.run_once.return_value = summary

                with mock.patch.object(cli, "EmbeddingWorker", return_value=worker):
                    code, _, _ = run_main([], {"DATABASE_URL": DEV_URL}, mock.Mock(return_value=mock.Mock()))

                self.assertEqual(code, expected)

    def test_최대_시도_횟수_옵션이_워커_설정에_반영된다(self):
        worker = mock.Mock()
        worker.run_once.return_value = RunSummary()

        with mock.patch.object(cli, "EmbeddingWorker", return_value=worker) as worker_cls:
            run_main(["--max-attempts", "2"], {"DATABASE_URL": DEV_URL}, mock.Mock(return_value=mock.Mock()))

        config = worker_cls.call_args.args[3]
        self.assertEqual(config.max_attempts, 2)

    def test_dotenv를_읽지_않는다(self):
        # .env의 DATABASE_URL은 운영 값이다 — 이 스크립트가 .env를 자동으로 읽으면 실수로 운영 DB에 쓰게 된다
        with open(cli.__file__, encoding="utf-8") as f:
            source = f.read()
        self.assertNotIn("load_dotenv", source)
        self.assertNotIn("import dotenv", source)
        self.assertNotIn("from dotenv", source)


class FormatSummaryTest(unittest.TestCase):
    def test_결과별_개수와_멈춘_이유를_보여준다(self):
        text = cli.format_summary(RunSummary(done=2, unchanged=1, retried=1, failed=1, abandoned=1, stopped="연속 3건 실패"))

        for expected in ("처리 5건", "완료 2", "이미 최신 1", "재시도 대기 1", "실패 1", "멈춘 워커의 작업을 실패 처리 1", "연속 3건 실패"):
            self.assertIn(expected, text)

    def test_멈춘_이유가_없으면_한_줄이다(self):
        self.assertEqual(len(cli.format_summary(RunSummary(done=1)).splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
