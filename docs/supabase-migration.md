# inote-ai DB 이전 — Neon → Supabase (BE DB에 합치기)

> AI 서버 쪽 작업만 정리. 전체 계획·Supabase 연결 방식·위험 요소는 `inote-server` 레포의
> `docs/supabase-migration.md` 참고. Notion 정리용 원본.
> 작성: 2026-09-30 / 상태: 🟡 준비 중 (이전 시작 전)

---

## 1. 결정

- AI 전용 DB(Neon `ep-wispy-moon`)를 없애고, BE용 Supabase 프로젝트(`inote-prod`)에 **합친다**.
- AI 테이블 2개(`conversation_sessions`, `conversations`)는 `public`이 아니라 **별도 스키마 `ai`** 에 둔다.
  - 이유: BE의 Prisma가 `db push`를 할 때 자기 스키마(`public`)에 없는 테이블을 지우려 할 수 있어서 격리.
  - 부수 효과: Supabase REST API는 기본으로 `public`만 노출하므로 AI 대화 기록이 API로 공개될 위험도 줄어든다.

## 2. 현재 상태

- 테이블: `conversation_sessions`(세션, 5행), `conversations`(대화, 16행) — 약 7.8MB.
- 코드: `app/db/postgres.py`의 `get_connection()`이 `psycopg.connect(DATABASE_URL, connect_timeout=10, prepare_threshold=None)`.
- 테이블 이름을 SQL에 직접 쓰는 곳: `app/routers/sessions.py`, `app/routers/conversations.py`, (진단) `app/main.py`의 `/health/db`.
- 스키마 정의: `schema.sql` (현재 `public` 기준).

## 3. 작업 리스트

### 준비
- [ ] Supabase `inote-prod`·`inote-dev`에 **`ai` 스키마** 생성 (`create schema if not exists ai;`)
- [ ] `schema.sql`을 `ai` 스키마용으로 수정 (테이블·인덱스·FK에 `ai.` 접두사), `gen_random_uuid()` 사용 가능 여부 확인
- [ ] Neon AI DB 백업 덤프 저장 (롤백용, 커밋하지 않음)

### 코드
- [ ] SQL의 테이블 이름에 `ai.` 접두사 추가 (`sessions.py`, `conversations.py`, `/health/db`의 테이블 확인 쿼리도 `ai` 스키마 기준으로)
- [ ] (대안 검토) `search_path`로 처리하면 SQL 수정이 없지만, 트랜잭션 풀러(6543)는 접속 시 `options` 파라미터를 받지 않을 수 있어 **명시적 `ai.` 접두사를 권장**
- [ ] 로컬에서 `inote-dev`의 `ai` 스키마로 동작 확인

### 데이터
- [ ] Neon AI DB → `inote-prod`의 `ai` 스키마로 이전 (세션 5, 대화 16) — FK 순서: 세션 → 대화
- [ ] 행 수 대조 (세션 5 / 대화 16)

### 환경 변수
- [ ] Vercel `DATABASE_URL` → `inote-prod` **Transaction pooler**(포트 6543) 주소, Redeploy
- [ ] 로컬 `inote-ai/.env`의 `DATABASE_URL` → `inote-dev` 주소

### 검증
- [ ] `/health/db` = `ok` (접속 + `ai` 테이블 2개 존재)
- [ ] 세션 생성 → 목록 조회 → 대화 저장 → 삭제 흐름 (이전에 Vercel에서 쓴 것과 같은 시험)
- [ ] Vercel 로그에 에러 없음 (`request_id`로 검색)

### 마무리
- [ ] Neon AI DB는 안정화 후 정리(삭제)
- [ ] `README.md`·`CLAUDE.md`·`docs/vercel-migration.md`의 DB 정보 최신화

## 4. 진행 로그

| 날짜 | 한 일 | 결과·메모 |
|---|---|---|
| 2026-09-30 | 합치기 결정, `ai` 스키마 분리 방침, 문서 작성 | 준비 완료 |

## 5. 트러블슈팅 (겪으면 채우기)

-
