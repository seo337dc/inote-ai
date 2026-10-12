import json
import os
import unittest
from unittest import mock

import httpx

from app.services.embedder_client import EXPECTED_DIM, EmbedderClient, EmbedderUnavailable, expected_model


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


if __name__ == "__main__":
    unittest.main()
