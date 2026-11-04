import json
import os
import unittest
from unittest import mock

import httpx

from app.services.embedder_client import (
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT,
    EXPECTED_DIM,
    EmbedderClient,
    EmbedderUnavailable,
    configured_retries,
    configured_timeout,
    expected_model,
    get_embedder,
)


def vector(dim=EXPECTED_DIM):
    return [0.0] * (dim - 1) + [1.0]


def client_with(handler, **kwargs):
    return EmbedderClient(base_url="http://embedder.test", secret="s3cret", transport=httpx.MockTransport(handler), **kwargs)


class EmbedderClientTest(unittest.TestCase):
    def test_정상_응답을_돌려주고_요청_모양이_맞다(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["secret"] = request.headers.get("x-internal-secret")
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"model": "nlpai-lab/KURE-v1", "dim": EXPECTED_DIM, "embeddings": [vector(), vector()]})

        result = client_with(handler).embed(["하나", "둘"])

        self.assertEqual(seen["url"], "http://embedder.test/embed")
        self.assertEqual(seen["secret"], "s3cret")
        self.assertEqual(seen["body"], {"inputs": ["하나", "둘"]})
        self.assertEqual((result.model, result.dim, len(result.embeddings)), ("nlpai-lab/KURE-v1", EXPECTED_DIM, 2))

    def test_주소_끝의_슬래시는_무시한다(self):
        def handler(request):
            self.assertEqual(request.url.path, "/embed")
            return httpx.Response(200, json={"model": "m", "dim": EXPECTED_DIM, "embeddings": [vector()]})

        EmbedderClient(base_url="http://embedder.test/", secret="s", transport=httpx.MockTransport(handler)).embed(["a"])

    def test_주소가_없으면_사용할_수_없다(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(EmbedderUnavailable):
                EmbedderClient().embed(["a"])

    def test_주소와_비밀키는_환경_변수에서_읽는다(self):
        with mock.patch.dict(os.environ, {"EMBEDDER_URL": "http://from-env.test", "EMBEDDER_SECRET": "env-secret"}, clear=True):
            seen = {}

            def handler(request):
                seen["host"], seen["secret"] = request.url.host, request.headers["x-internal-secret"]
                return httpx.Response(200, json={"model": "m", "dim": EXPECTED_DIM, "embeddings": [vector()]})

            EmbedderClient(transport=httpx.MockTransport(handler)).embed(["a"])
        self.assertEqual(seen, {"host": "from-env.test", "secret": "env-secret"})

    def test_200이_아니면_사용할_수_없다(self):
        for status in (401, 422, 500, 503):
            with self.subTest(status=status):
                with self.assertRaises(EmbedderUnavailable):
                    client_with(lambda r, s=status: httpx.Response(s, json={"detail": "x"})).embed(["a"])

    def test_연결_실패와_시간_초과는_사용할_수_없다(self):
        def refused(request):
            raise httpx.ConnectError("refused")

        def slow(request):
            raise httpx.ReadTimeout("slow")

        for handler in (refused, slow):
            with self.subTest(handler=handler.__name__):
                with self.assertRaises(EmbedderUnavailable):
                    client_with(handler).embed(["a"])

    def test_JSON이_아니거나_필드가_빠진_응답은_사용할_수_없다(self):
        bad = [
            httpx.Response(200, content=b"not json"),
            httpx.Response(200, json={"model": "m", "dim": EXPECTED_DIM}),  # embeddings 없음
            httpx.Response(200, json=["list"]),
        ]
        for res in bad:
            with self.subTest(res=res.content[:20]):
                with self.assertRaises(EmbedderUnavailable):
                    client_with(lambda r, x=res: x).embed(["a"])

    def test_차원이_다르거나_개수가_안_맞는_응답은_사용할_수_없다(self):
        cases = {
            "dim=3": {"model": "m", "dim": 3, "embeddings": [[0.1, 0.2, 0.3]]},
            "개수 불일치": {"model": "m", "dim": EXPECTED_DIM, "embeddings": [vector(), vector()]},
            "벡터 길이가 dim과 다름": {"model": "m", "dim": EXPECTED_DIM, "embeddings": [[0.1, 0.2]]},
        }
        for name, body in cases.items():
            with self.subTest(name):
                with self.assertRaises(EmbedderUnavailable):
                    client_with(lambda r, b=body: httpx.Response(200, json=b)).embed(["a"])


class ExpectedModelTest(unittest.TestCase):
    def test_환경_변수가_없으면_KURE_v1(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(expected_model(), "nlpai-lab/KURE-v1")

    def test_환경_변수로_바꿀_수_있다(self):
        with mock.patch.dict(os.environ, {"EMBEDDER_MODEL": "other/model"}, clear=True):
            self.assertEqual(expected_model(), "other/model")


def ok_response():
    return httpx.Response(200, json={"model": "m", "dim": EXPECTED_DIM, "embeddings": [vector()]})


class RetryTest(unittest.TestCase):
    """재시도 횟수는 설정값이다. 연결 실패·시간 초과·5xx·429만 다시 시도하고, 다시 해도 같은 실패(4xx·이상한 응답)는 바로 포기한다."""

    @staticmethod
    def counting(responder):
        calls = []

        def handler(request):
            calls.append(request)
            return responder(len(calls))

        return calls, handler

    def refuse(self, n):
        raise httpx.ConnectError("refused")

    def test_기본은_재시도하지_않는다(self):
        calls, handler = self.counting(self.refuse)

        with self.assertRaises(EmbedderUnavailable):
            client_with(handler).embed(["a"])

        self.assertEqual(len(calls), 1)

    def test_연결_실패는_정해진_횟수까지_다시_시도하고_성공하면_결과를_돌려준다(self):
        calls, handler = self.counting(lambda n: self.refuse(n) if n < 3 else ok_response())

        result = client_with(handler, retries=2, retry_delay=0).embed(["a"])

        self.assertEqual(len(calls), 3)
        self.assertEqual(result.dim, EXPECTED_DIM)

    def test_계속_실패하면_사용할_수_없고_호출_횟수는_처음_더하기_재시도_횟수(self):
        calls, handler = self.counting(self.refuse)

        with self.assertRaises(EmbedderUnavailable):
            client_with(handler, retries=2, retry_delay=0).embed(["a"])

        self.assertEqual(len(calls), 3)

    def test_시간_초과도_재시도한다(self):
        def slow(n):
            raise httpx.ReadTimeout("slow")

        calls, handler = self.counting(slow)

        with self.assertRaises(EmbedderUnavailable):
            client_with(handler, retries=1, retry_delay=0).embed(["a"])

        self.assertEqual(len(calls), 2)

    def test_5xx와_429는_재시도하고_4xx는_재시도하지_않는다(self):
        for status, expected_calls in ((500, 3), (503, 3), (429, 3), (401, 1), (422, 1)):
            with self.subTest(status=status):
                calls, handler = self.counting(lambda n, s=status: httpx.Response(s, json={"detail": "x"}))

                with self.assertRaises(EmbedderUnavailable):
                    client_with(handler, retries=2, retry_delay=0).embed(["a"])

                self.assertEqual(len(calls), expected_calls)

    def test_이상한_응답은_재시도하지_않는다(self):
        bad = {
            "JSON이 아님": httpx.Response(200, content=b"not json"),
            "차원이 다름": httpx.Response(200, json={"model": "m", "dim": 3, "embeddings": [[0.1, 0.2, 0.3]]}),
        }
        for name, res in bad.items():
            with self.subTest(name):
                calls, handler = self.counting(lambda n, r=res: r)

                with self.assertRaises(EmbedderUnavailable):
                    client_with(handler, retries=2, retry_delay=0).embed(["a"])

                self.assertEqual(len(calls), 1)

    def test_재시도_사이에는_시도_횟수에_비례해_기다린다(self):
        calls, handler = self.counting(self.refuse)

        with mock.patch("app.services.embedder_client.time.sleep") as sleep:
            with self.assertRaises(EmbedderUnavailable):
                client_with(handler, retries=2, retry_delay=2.0).embed(["a"])

        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2.0, 4.0])  # 마지막 실패 뒤에는 기다리지 않는다

    def test_주소가_없으면_재시도하지_않고_바로_사용할_수_없다(self):
        with mock.patch("app.services.embedder_client.time.sleep") as sleep:
            with self.assertRaises(EmbedderUnavailable):
                EmbedderClient(base_url="", secret="s", retries=3, retry_delay=1.0).embed(["a"])

        sleep.assert_not_called()

    def test_음수_재시도는_0으로_본다(self):
        calls, handler = self.counting(self.refuse)

        with self.assertRaises(EmbedderUnavailable):
            client_with(handler, retries=-1, retry_delay=0).embed(["a"])

        self.assertEqual(len(calls), 1)


class UnreachableFlagTest(unittest.TestCase):
    """unreachable: 서비스에 아예 닿지 못함(주소 없음·연결 실패·시간 초과) — 대기열 워커가 시도 횟수를 세지 않고 멈추는 기준."""

    def caught(self, client):
        with self.assertRaises(EmbedderUnavailable) as ctx:
            client.embed(["a"])
        return ctx.exception

    def test_주소가_없거나_연결_실패_시간_초과는_unreachable(self):
        def refused(request):
            raise httpx.ConnectError("refused")

        def slow(request):
            raise httpx.ReadTimeout("slow")

        self.assertTrue(self.caught(EmbedderClient(base_url="", secret="s")).unreachable)
        self.assertTrue(self.caught(client_with(refused)).unreachable)
        self.assertTrue(self.caught(client_with(slow)).unreachable)

    def test_응답이_왔다면_오류_응답이나_이상한_응답이어도_unreachable이_아니다(self):
        cases = {
            "500": httpx.Response(500, json={"detail": "x"}),
            "429": httpx.Response(429, json={"detail": "x"}),
            "401": httpx.Response(401, json={"detail": "x"}),
            "JSON이 아님": httpx.Response(200, content=b"not json"),
            "차원이 다름": httpx.Response(200, json={"model": "m", "dim": 3, "embeddings": [[0.1, 0.2, 0.3]]}),
        }
        for name, res in cases.items():
            with self.subTest(name):
                self.assertFalse(self.caught(client_with(lambda r, x=res: x)).unreachable)

    def test_재시도를_다_쓴_뒤에도_unreachable_표시가_유지된다(self):
        def refused(request):
            raise httpx.ConnectError("refused")

        self.assertTrue(self.caught(client_with(refused, retries=2, retry_delay=0)).unreachable)


class ConfigTest(unittest.TestCase):
    def env(self, **values):
        return mock.patch.dict(os.environ, values, clear=True)

    def test_제한_시간은_환경_변수로_정하고_없거나_잘못되면_기본값(self):
        with self.env():
            self.assertEqual(configured_timeout(), DEFAULT_TIMEOUT)
        with self.env(EMBEDDER_TIMEOUT="7.5"):
            self.assertEqual(configured_timeout(), 7.5)
        for bad in ("abc", "0", "-1", "301", " "):
            with self.subTest(bad=bad), self.env(EMBEDDER_TIMEOUT=bad):
                self.assertEqual(configured_timeout(), DEFAULT_TIMEOUT)

    def test_재시도_횟수는_환경_변수로_정하고_없거나_잘못되면_기본값(self):
        with self.env():
            self.assertEqual(configured_retries(), DEFAULT_RETRIES)
        with self.env(EMBEDDER_RETRIES="3"):
            self.assertEqual(configured_retries(), 3)
        for bad in ("abc", "-1", "6", "1.5", " "):
            with self.subTest(bad=bad), self.env(EMBEDDER_RETRIES=bad):
                self.assertEqual(configured_retries(), DEFAULT_RETRIES)

    def test_의존성으로_만든_클라이언트가_설정값을_따른다(self):
        with self.env(EMBEDDER_URL="http://e.test", EMBEDDER_TIMEOUT="7", EMBEDDER_RETRIES="2"):
            embedder = get_embedder()

        self.assertEqual((embedder._timeout, embedder._retries), (7.0, 2))


if __name__ == "__main__":
    unittest.main()
