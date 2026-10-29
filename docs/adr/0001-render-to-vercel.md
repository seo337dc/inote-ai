# inote-ai 호스팅 이전 — Render → Vercel

> 이 레포(`inote-ai`)의 이전 기록. 전체 배경·원인·결정은 `inote` 레포의 `docs/adr/0003-hosting-migration.md`,
> DB 이전(Neon → Supabase)은 `inote-server` 레포의 `docs/adr/0001-neon-to-supabase.md` 참고. Notion 정리용 원본.
> 시작: 2026-09-30 / 상태: 🟡 구성 완료, Vercel 배포 대기

---

## 1. 왜 옮기나 (요약)

Render 무료 웹 서비스 2개를 10분마다 깨워 두다가 월 750시간 한도를 넘겨 **정지(Suspended)** 됐다
("Free usage limit reached… suspended until the next billing period"). AI 서버는 요약·채팅에만 쓰여서
상시 서버가 필요 없으므로, 서버리스인 **Vercel Python 함수**로 옮긴다.

## 2. Vercel Hobby 제한 (공식 문서 확인, 2026-09-30)

| 항목 | 값 | 우리에게 의미 |
|---|---|---|
| 함수 최대 실행 시간 | 기본·최대 300초 (Fluid compute, 기본 활성). 스트리밍 응답 시간도 포함 | 채팅·요약 스트리밍은 300초 안에 끝남 |
| Python FastAPI 시간 설정 | `vercel.json`의 `functions`에 **진입점 파일**(예: `app/main.py`)을 키로 `maxDuration` (앱 전체가 함수 1개로 빌드됨) | 기본값(300초)으로 충분해 설정 안 함 |
| 메모리 | 2GB / 1 vCPU | 충분 |
| 번들 크기 | 일반 250MB, Python 500MB(비압축) | 의존성 약 32MB, 앱 44KB로 여유 |
| 요청·응답 본문 | 4.5MB | 텍스트라 문제 없음 |
| 월 포함 사용량 | Active CPU 4 CPU-hrs, 메모리 360 GB-hrs, 함수 호출 100만 건 | **I/O 대기(Groq·DB 응답 기다리는 시간)는 Active CPU에 미포함**. 초과 시 약 30일간 해당 기능 못 씀 |
| 용도 | 비상업·개인 프로젝트만 | 해당 |
| 주의 | 사용량은 계정 단위 합산 — FE(Next.js 서버 함수)와 한도를 나눠 씀 | 트래픽이 커지면 재점검 |
| 웹소켓 | 미지원 | 우리는 SSE만 사용 |
| Python 버전 | 3.12(기본) / 3.13 / 3.14 | `.python-version`으로 3.12 고정 |
| 진입점 자동 인식 | `app.py`·`index.py`·`server.py`·`main.py` 또는 `app/`·`src/` 안의 같은 이름, 변수명 `app` | `app/main.py`의 `app` 그대로 인식 → `vercel.json` 불필요 |

## 3. 작업 체크리스트

- [x] 실행 시간 제한 공식 문서 확인 (2026-09-30)
- [x] `.python-version`(3.12) 추가 — Render에서 Python 3.14 기본값 때문에 `pydantic-core` 빌드가 실패했던 적이 있어 미리 고정
- [x] `.vercelignore` 추가 — Python 함수는 프로젝트 파일을 전부 번들에 넣으므로 `venv/`, `.env`, 문서 파일 제외 필수
- [x] DB 연결 보강 (`app/db/postgres.py`): `connect_timeout=10`, `prepare_threshold=None`(풀러 호환). 요청마다 `with get_connection()`로 열고 닫는 구조는 그대로 확인
- [x] 로컬 검증(uvicorn): `/health` 200, `/docs` 200, 비밀키 없는 `/summarize` 401, Neon pooled 주소로 DB 조회 성공
- [x] Vercel 프로젝트 생성(GitHub `inote-ai` 저장소 가져오기, Framework FastAPI 자동 인식) — 배포 주소 `https://inote-ai-cyan.vercel.app` (2026-09-30)
- [x] 환경 변수 입력 (아래 표) — `PYTHON_VERSION`은 Render 전용이라 입력하지 않음
- [x] 배포 후 검증(일부): `/health`, `/docs`, `/summarize`(200, 한국어 요약), 채팅 스트리밍(SSE 정상), CORS(`https://inote-main.vercel.app` 허용), DB 조회·저장(세션 생성 → 목록 조회 → 삭제 정리) (2026-09-30)
- [ ] 배포 후 검증(BE 필요): 글에 연결된 채팅(BE에서 작성자 확인), 독서 기록 AI(카카오 도서 검색 포함) — Render BE가 재개된 뒤
- [x] 진단용 엔드포인트 `/health/db` 추가(internal 전용) — 접속 성공 여부, 에러 종류, `DATABASE_URL` 구조(비밀번호 제외), AI 테이블 존재 여부 확인
- [x] 에러 로그 정책 적용 — 처리되지 않은 에러와 5xx는 traceback·`request_id`(`x-vercel-id`)와 함께 로그에 남김, 스트리밍 중 실패도 기록(요청 본문·헤더·키는 기록하지 않음)
- [x] BE(Render)의 `INOTE_AI_URL`을 Vercel 주소로 교체 (2026-09-30)
- [ ] FE(Vercel)의 `NEXT_PUBLIC_AI_API_URL`을 새 주소로 교체·재배포 — 확인 필요
- [x] Render의 `inote-ai` 서비스, cron-job의 "inote ai 서버 슬립 해제" 작업 삭제 (2026-09-30)
- [ ] 마무리: `render.yaml`·`runtime.txt` 정리(더 이상 쓰지 않음), `README.md`·`CLAUDE.md`의 배포 정보 최신화

## 4. 환경 변수 (값은 적지 않는다)

| 변수 | 비고 |
|---|---|
| `GROQ_API_KEY` | |
| `DATABASE_URL` | Neon **pooled** 주소(호스트에 `-pooler`) |
| `INTERNAL_SECRET` | BE와 동일한 값 |
| `INOTE_SERVER_URL` | BE 주소(Render) + `/api/v1` |
| `ALLOWED_ORIGINS` | FE 주소 (쉼표로 여러 개) |
| `KAKAO_REST_API_KEY` | 독서 기록 책 검색용 |

## 5. 진행 로그

| 날짜 | 한 일 | 결과·메모 |
|---|---|---|
| 2026-09-30 | Vercel Hobby 제한 확인, 배포 파일 추가, DB 연결 보강, 로컬 검증 | 로컬 Python은 3.9라 3.12 실행은 Vercel 배포에서 확인 필요 |
| 2026-09-30 | Vercel 배포, 요약·스트리밍·CORS 확인. `/sessions`가 500 | 원인: `DATABASE_URL`이 BE(`ep-fancy-bar`)의 DB를 가리켜 AI 테이블이 없었음 |
| 2026-09-30 | `DATABASE_URL`을 AI 전용 DB(`ep-wispy-moon`)로 교체·재배포 | `/health/db` ok, 세션 저장·조회·삭제 모두 정상. Python 3.12.14에서 동작 확인 |

## 6. 트러블슈팅 (겪으면 채우기)

- (사전 메모) Python 3.14가 기본이면 `pydantic-core` 빌드 실패 → 3.12 고정.
- (사전 메모) `venv/`가 번들에 들어가면 용량 제한·배포 지연 → `.vercelignore`.
- **DB 조회만 500 (`/sessions`)**: 요약·스트리밍은 정상인데 DB를 읽는 API만 `Internal Server Error`.
  - 단서: 환경 변수를 다시 넣은 뒤 실행 시간이 9ms → 약 580ms로 바뀜 = 이제 접속은 하고 있다는 뜻.
  - 원인: Vercel `DATABASE_URL`에 **BE의 DB 주소**(다른 Neon 프로젝트)가 들어 있어서 접속은 되지만 `conversation_sessions`·`conversations` 테이블이 없었음.
  - 해결: AI 서버 전용 DB 주소로 교체 후 재배포.
  - 교훈: 서비스마다 DB가 다를 수 있으니 환경 변수는 **호스트까지** 비교해서 옮긴다. `/health/db`가 호스트·테이블 존재 여부를 바로 알려준다.
- **Vercel 로그에 에러가 안 보임**: 한 인스턴스가 요청을 동시에 처리(Fluid compute)해서 에러 로그가 실제 요청이 아닌 다른 요청 행에 붙어 보이거나 "No logs found for this request"로 나옴.
  - 대응: 에러 로그에 `request_id`(`x-vercel-id`)를 넣고, 500 응답 본문에도 같은 값을 돌려줘서 로그 검색창에서 그 값으로 찾는다. 로그만 믿지 말고 `/health/db`로 직접 확인한다.
- **비밀번호가 채팅에 노출됨**: DB 주소를 붙여 넣다 비밀번호가 그대로 노출 → Neon에서 비밀번호 재설정. 주소를 공유할 때는 비밀번호를 `***`로 가린다.

## 7. 참고 자료

- Vercel Python 런타임: https://vercel.com/docs/functions/runtimes/python
- Vercel 함수 제한: https://vercel.com/docs/functions/limitations
- Vercel 최대 실행 시간 설정: https://vercel.com/docs/functions/configuring-functions/duration
- Vercel Hobby 플랜: https://vercel.com/docs/plans/hobby
