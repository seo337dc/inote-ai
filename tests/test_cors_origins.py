"""CORS 허용 출처 해석 테스트 — 쉼표 뒤 공백 같은 입력 실수가 조용한 CORS 거절로 이어지지 않는지.
(운영에서 `ALLOWED_ORIGINS`를 `https://a.app, http://localhost:3011`로 넣었더니 두 번째 출처만 거절됐던 문제)"""

import importlib
import os
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from app.cors_origins import DEFAULT_ORIGINS, parse_allowed_origins

PROD = "https://inote-main.vercel.app"
LOCAL = "http://localhost:3011"


class ParseAllowedOriginsTest(unittest.TestCase):
    def test_env_missing_allows_only_the_local_default(self):
        self.assertEqual(parse_allowed_origins(None), [DEFAULT_ORIGINS])
        self.assertEqual(DEFAULT_ORIGINS, LOCAL)

    def test_the_production_case_space_after_comma(self):
        # 실제로 문제였던 값 — 두 번째 출처 앞의 공백이 그대로 남으면 브라우저의 출처와 달라진다
        result = parse_allowed_origins(f"{PROD}, {LOCAL}")
        self.assertEqual(result, [PROD, LOCAL])
        self.assertNotIn(f" {LOCAL}", result)

    def test_plain_comma_separated_values_still_work(self):
        self.assertEqual(parse_allowed_origins(f"{PROD},{LOCAL}"), [PROD, LOCAL])
        self.assertEqual(parse_allowed_origins(LOCAL), [LOCAL])

    def test_any_kind_of_surrounding_whitespace_is_removed(self):
        self.assertEqual(parse_allowed_origins(f"  {PROD}  ,\t{LOCAL}\n"), [PROD, LOCAL])
        self.assertEqual(parse_allowed_origins(f"{PROD} ,   {LOCAL}   "), [PROD, LOCAL])
        self.assertEqual(parse_allowed_origins(f"{PROD}\n,{LOCAL}"), [PROD, LOCAL])

    def test_empty_items_are_dropped(self):
        self.assertEqual(parse_allowed_origins(f"{PROD},,{LOCAL},"), [PROD, LOCAL])
        self.assertEqual(parse_allowed_origins(f", ,{PROD}, ,"), [PROD])

    def test_set_but_unusable_means_nothing_is_allowed(self):
        # 설정 실수를 열어 두지 않는다 — 기본값(로컬)으로 되돌리지도 않는다
        for raw in ["", "   ", ",", " , , ", "\n"]:
            self.assertEqual(parse_allowed_origins(raw), [], repr(raw))

    def test_wrapping_quotes_are_removed(self):
        self.assertEqual(parse_allowed_origins(f'"{LOCAL}"'), [LOCAL])
        self.assertEqual(parse_allowed_origins(f"'{PROD}', \"{LOCAL}\""), [PROD, LOCAL])
        self.assertEqual(parse_allowed_origins(f'"  {LOCAL}  "'), [LOCAL])

    def test_quotes_in_the_middle_or_unbalanced_are_not_touched(self):
        self.assertEqual(parse_allowed_origins('"http://a'), ['"http://a'])
        self.assertEqual(parse_allowed_origins("http://a\"b"), ["http://a\"b"])
        self.assertEqual(parse_allowed_origins("\"http://a'"), ["\"http://a'"])

    def test_trailing_slash_is_removed_because_browser_origins_have_no_path(self):
        self.assertEqual(parse_allowed_origins(f"{PROD}/, {LOCAL}//"), [PROD, LOCAL])

    def test_duplicates_are_dropped_keeping_first_order(self):
        self.assertEqual(parse_allowed_origins(f"{LOCAL}, {PROD}, {LOCAL}/ ,{PROD}"), [LOCAL, PROD])

    def test_other_parts_of_the_origin_are_kept_as_is(self):
        self.assertEqual(parse_allowed_origins("http://192.168.0.5:3011, https://sub.example.com:8443"),
                         ["http://192.168.0.5:3011", "https://sub.example.com:8443"])


def preflight(client: TestClient, origin: str):
    return client.options(
        "/chat/stream",
        headers={"Origin": origin, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"},
    )


class CorsPreflightTest(unittest.TestCase):
    """해석한 목록을 실제 CORS 미들웨어에 넣었을 때, 브라우저의 사전 요청이 어떻게 답받는지."""

    def client_for(self, raw):
        app = FastAPI()
        app.add_middleware(CORSMiddleware, allow_origins=parse_allowed_origins(raw), allow_methods=["*"], allow_headers=["*"])
        return TestClient(app)

    def test_both_origins_are_allowed_when_the_value_has_a_space_after_the_comma(self):
        client = self.client_for(f"{PROD}, {LOCAL}")
        for origin in (PROD, LOCAL):
            res = preflight(client, origin)
            self.assertEqual(res.status_code, 200, origin)
            self.assertEqual(res.headers.get("access-control-allow-origin"), origin)

    def test_the_old_split_would_have_rejected_the_local_origin(self):
        # 회귀 확인: 예전 방식(strip 없이 split)이면 로컬 출처가 거절된다 — 이 시험이 실제 문제를 재현한다
        app = FastAPI()
        app.add_middleware(CORSMiddleware, allow_origins=f"{PROD}, {LOCAL}".split(","), allow_methods=["*"], allow_headers=["*"])
        res = preflight(TestClient(app), LOCAL)
        self.assertEqual(res.status_code, 400)
        self.assertNotIn("access-control-allow-origin", res.headers)

    def test_origins_not_in_the_list_are_still_rejected(self):
        client = self.client_for(f"{PROD}, {LOCAL}")
        for origin in ("http://localhost:3000", "https://evil.example.com", "http://localhost:3011.evil.com", PROD.replace("https", "http")):
            res = preflight(client, origin)
            self.assertEqual(res.status_code, 400, origin)
            self.assertNotIn("access-control-allow-origin", res.headers, origin)

    def test_unusable_value_allows_no_origin_at_all(self):
        client = self.client_for("  ,  ")
        self.assertEqual(preflight(client, LOCAL).status_code, 400)
        self.assertEqual(preflight(client, PROD).status_code, 400)

    def test_env_missing_allows_the_local_default_only(self):
        client = self.client_for(None)
        self.assertEqual(preflight(client, LOCAL).status_code, 200)
        self.assertEqual(preflight(client, PROD).status_code, 400)


class MainWiringTest(unittest.TestCase):
    """app.main이 이 해석 함수를 실제로 쓰는지 — 값을 환경변수로 넣고 앱을 다시 읽어서 확인한다."""

    def tearDown(self):
        import app.main
        importlib.reload(app.main)  # 환경을 되돌린 상태로 복원

    def reload_main(self, value):
        import app.main
        with mock.patch.dict(os.environ, {"ALLOWED_ORIGINS": value}):
            return importlib.reload(app.main)

    def test_main_allows_the_local_origin_when_the_env_has_a_space_after_the_comma(self):
        main = self.reload_main(f"{PROD}, {LOCAL}")
        self.assertEqual(main.allowed_origins, [PROD, LOCAL])
        res = preflight(TestClient(main.app), LOCAL)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers.get("access-control-allow-origin"), LOCAL)

    def test_main_still_rejects_unlisted_origins(self):
        main = self.reload_main(f"{PROD}, {LOCAL}")
        self.assertEqual(preflight(TestClient(main.app), "http://localhost:3000").status_code, 400)


if __name__ == "__main__":
    unittest.main()
