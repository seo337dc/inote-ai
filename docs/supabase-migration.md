# inote-ai DB 이전 — Neon → Supabase (AI 전용 프로젝트로 분리)

> AI 서버 쪽 작업만 정리. 전체 계획·Supabase 연결 방식·위험 요소는 `inote-server` 레포의
> `docs/supabase-migration.md` 참고. Notion 정리용 원본.
> 작성: 2026-09-30 / 최종 갱신: 2026-10-02 / 상태: 🟡 데이터 이전·로컬 검증 완료, Vercel 환경변수 교체 전

---

## 1. 결정

- AI 전용 DB(Neon `ep-wispy-moon`)를 **AI 전용 Supabase 프로젝트 `inote-ai`**(East US/Ohio, BE 프로젝트와 같은 리전)로 옮긴다. 테이블은 원래대로 `public`에 둔다.
- 처음에는 BE 프로젝트에 `ai` 스키마로 합치는 안(2026-09-30)이었으나 **2026-10-02에 분리로 바꿨다.**
  - 같은 프로젝트에 두면 AI 서버(Vercel)가 `postgres` 계정으로 접속하게 되어, AI 서버가 뚫리거나 `DATABASE_URL`이 새면 BE의 사용자·OAuth 토큰·글·가계부까지 읽힌다. 스키마 분리는 보안 경계가 아니다.
  - BE의 마이그레이션·`db push`·비밀번호 로테이션이 AI에 영향을 주지 않는다.
  - BE는 상시 서버(Session pooler 5432), AI는 서버리스(Transaction pooler 6543)라 접속 방식과 연결 한도를 따로 가져가는 게 자연스럽다.
  - 코드가 단순해진다 — `ai.` 접두사 없이 원래 SQL 그대로.
- 감수한 점: 무료 프로젝트 2개를 모두 사용, **프로젝트마다 1주일 미사용 시 자동 일시중지**(AI 채팅을 안 쓰면 AI DB만 멈춤 → 아래 "주기 호출" 참고), 접속 주소·비밀번호가 하나 더 늘어남.

## 2. 현재 상태

- 테이블: `conversation_sessions`(세션, 5행), `conversations`(대화, 16행) — 새 프로젝트 `public`에 복원 완료, 내용 해시 동일, RLS 켬.
- 코드: `app/db/postgres.py`의 `get_connection()`이 `psycopg.connect(DATABASE_URL, connect_timeout=10, prepare_threshold=None)` — **변경 없음**(`prepare_threshold=None`이 Transaction pooler 호환을 코드에서 처리).
- 스키마 정의: `schema.sql` (`public` 기준 + RLS 활성화 추가).
- 연결: 로컬 `inote-ai/.env`는 새 프로젝트를 가리킴(단, `?pgbouncer=true`·따옴표는 제거 필요 — 트러블슈팅 참고). Vercel은 아직 Neon.

## 3. 작업 리스트

### 준비
- [x] Supabase에 AI 전용 프로젝트 `inote-ai` 생성 (Ohio, 무료)
- [x] Neon AI DB 백업 덤프 저장 (롤백용, 커밋하지 않음)
- [x] `schema.sql`에 RLS 활성화 추가 (`public` 기준 유지)

### 데이터
- [x] 새 프로젝트에 테이블 생성 + Neon AI 백업 복원 (세션 5, 대화 16) — FK 순서: 세션 → 대화, 한 트랜잭션
- [x] 행 수 대조 + 내용 해시 대조 (동일)
- [x] 이전 직전 Neon과 행 수·최신 시각 재확인 (그 사이 새 대화 없음: 5 / 16)

### 검증 (로컬, 새 프로젝트의 Transaction pooler 6543)
- [x] `/health/db` = `ok`
- [x] 세션 목록(3건) → 대화 이력(8건) → 시험 세션 생성·저장·조회 → 삭제 흐름

### 환경 변수
- [ ] Vercel(inote-ai) `DATABASE_URL` → 새 프로젝트 **Transaction pooler**(포트 6543) 주소 — **저장만, Redeploy는 코드 푸시와 함께**
- [ ] Render BE의 `INOTE_AI_URL`이 Vercel AI 주소인지 확인
- [ ] 로컬 `inote-ai/.env`의 `DATABASE_URL`에서 `?pgbouncer=true`·따옴표 제거

### 배포·검증
- [ ] 배포 직전 Neon ↔ 새 프로젝트 행 수 재비교 (차이가 있으면 차이만 이전)
- [ ] 문서 커밋·푸시 → Vercel 배포
- [ ] 배포 후 `/health/db` = `ok`, 세션 생성 → 목록 → 대화 저장 → 삭제 흐름
- [ ] 운영 FE에서 AI 채팅·기존 대화 이력(세션 5건) 확인, Vercel 로그에 에러 없음 (`request_id`로 검색)

### 마무리
- [ ] 일시중지 방지: cron-job으로 AI `/health/db`(내부 시크릿 헤더 필요)를 주기적으로 호출 — 쿼리를 보내므로 DB 활동으로 잡힘
- [ ] BE 프로젝트에 남아 있는 `ai` 스키마(합치기 시도 때 복원한 사본) 삭제 — 안정화 후
- [ ] Neon AI DB는 안정화 후 정리(삭제), Render에 옛 AI 서비스가 있으면 일시중지·삭제
- [ ] `README.md`·`CLAUDE.md`·`docs/vercel-migration.md`의 DB 정보 최신화
- [ ] 접속 파일(`inote-server/.env.supabase-ai.local`)의 비밀번호 로테이션 고려

## 4. 진행 로그

| 날짜 | 한 일 | 결과·메모 |
|---|---|---|
| 2026-09-30 | 합치기 결정, `ai` 스키마 분리 방침, 문서 작성 | 준비 완료 |
| 2026-10-02 | BE 프로젝트의 `ai` 스키마에 복원·검증까지 진행 | 이후 분리로 방침 변경 — 이 사본은 정리 대상 |
| 2026-10-02 | 분리로 결정. 첫 새 프로젝트는 리전 혼선으로 삭제 후 Ohio로 재생성 | |
| 2026-10-02 | 새 프로젝트에 테이블 생성 + RLS + 백업 복원, 해시 동일. `ai.` 접두사 코드 수정은 되돌림 | 로컬에서 6543으로 health·목록·이력·저장·삭제 확인 |

## 5. 트러블슈팅

- **`tenant/user postgres.<ref> not found`** — 비밀번호 문제가 아니라 **ref나 풀러 호스트가 틀렸다**는 뜻. 스크린샷에서 ref를 읽다가 한 글자를 잘못 읽은 게 원인이었다. 확인 방법: `<ref>.supabase.co`가 DNS에 잡히는지 조회(비밀번호 불필요). 주소는 이미지에서 읽지 말고 Copy 버튼으로 복사한다.
- **`password authentication failed for user "postgres"`** — ref·호스트는 맞고 **비밀번호만 다르다**. 클립보드가 붙여넣기 전에 바뀌었거나 "Generate a password"로 다른 값이 설정된 경우. 직접 정한 비밀번호를 접속 파일에 넣는 쪽이 확실하다.
- **`invalid URI query parameter: "pgbouncer"`** — 대시보드 Connect 문자열 예시의 `?pgbouncer=true`는 **Prisma 전용**이다. psycopg(libpq)는 모르는 파라미터라 접속 전에 에러가 난다. 이 서버는 `prepare_threshold=None`을 코드에서 설정하므로 URL에 붙이지 않는다. 따옴표도 붙이지 않는다.
- **리전** — Vercel 서버 코드는 미국 동부(`iad1`)에서 돈다. DB가 서울이면 왕복 약 180~200ms라 쿼리가 많은 요청이 느려진다. 리전은 만든 뒤 바꿀 수 없으니 BE와 같은 Ohio(`us-east-2`)로 만든다.
- **`ai` 스키마 안에서 `search_path`로 처리** — Transaction pooler는 접속 시 `options` 파라미터를 받지 않을 수 있어 비추천. (분리로 바꾸면서 해당 없어짐)
- **GitHub 연결·Branching** — Supabase의 GitHub 연결은 `supabase/migrations` 자동 적용·브랜치별 임시 DB용이다. Prisma/psycopg로 마이그레이션하는 이 프로젝트들엔 불필요하니 연결하지 않는다.
