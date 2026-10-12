# inote-ai

`inote`(구 inote-blog)의 LLM/AI 전용 백엔드 서비스. 채팅, 대화 기록 저장, 저장 시 AI 자동 요약을
담당하며 로그인 로직은 없음 — FE가 이미 인증된 `user_id`를 실어 보내면 그대로 신뢰해서 데이터에
태깅만 한다.

왜 별도 서비스로 분리했는지, 전체 아키텍처는 `inote` 레포의 `PLANNING.md`/`STRATEGY.md` 참고.
전체 개발 이력은 [`DEV_LOG.md`](./DEV_LOG.md), 남은 로드맵은 [`CLAUDE.md`](./CLAUDE.md) 참고.

## 아키텍처

```
inote (FE, Next.js)
  ├─ inote-server (BE, NestJS)         — 글 CRUD, 인증, 자체 Neon DB
  └─ inote-ai (이 레포, FastAPI/Python)  — LLM 채팅, 대화 저장, AI 요약, 별도 Neon DB
```

`inote-ai` ↔ `inote-server`는 고정 시크릿 헤더(`x-internal-secret`)로 서로 내부 API를 호출한다
(예: 글 작성자 확인, 요약 콜백). DB는 완전히 분리돼 있어 직접 조인하지 않는다.

## 기능

- **LLM 채팅 스트리밍** — Groq API, SSE 스트리밍 (`/chat/stream`)
- **대화 세션 저장** — 글쓰기 세션(그 글에 묶임) / 일반 세션(자유 대화) 구분, 세션 목록·검색
- **저장 시 AI 자동 요약** — 글 발행 시 `inote-server`가 호출, 불릿 3~4개 반환
- **접근 제어** — 본인 글이 아니면 채팅 응답은 그대로 오되 대화 기록 저장/조회는 안 됨

## API

| Method | URL | 설명 |
|--------|-----|------|
| POST | `/chat/stream` | LLM 채팅 (SSE 스트리밍), 세션 upsert + 메시지 저장 |
| GET | `/conversations/{session_id}` | 세션의 대화 이력 조회 (`user_id` 필수) |
| GET | `/sessions` | 세션 목록 (`user_id` 필수, `q`로 검색) |
| POST | `/summarize` | 글 내용 → 불릿 요약 (내부 전용, `x-internal-secret`) |
| GET | `/health` | 헬스 체크 |

FastAPI가 기본 제공하는 Swagger UI를 그대로 씀 — 로컬 `http://localhost:8000/docs`, 배포
`https://inote-ai.onrender.com/docs` (OpenAPI 스키마는 `/openapi.json`).

## 스택

- Python + FastAPI + uvicorn
- PostgreSQL (Neon, `inote-server`와는 별도 DB) — `psycopg`
- Groq API (LLM 채팅·요약)
- httpx (내부 서비스 간 호출)

## 로컬 실행

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # 값 채우기 (아래 환경변수 참고)

uvicorn app.main:app --reload --port 8000
```

### 로컬 개발 DB (도커, Postgres + pgvector)

운영 AI DB(Supabase 별도 프로젝트)와 분리된 이 PC 안의 DB. 새 테이블·확장은 여기서 먼저 검증한다.

```bash
cp .env.dev.example .env.dev      # AI_DEV_DB_PASSWORD를 채운다: openssl rand -hex 16 (.env.dev는 git에 올리지 않는다)
docker compose -f docker-compose.dev.yml --env-file .env.dev up -d    # 처음 한 번 schema.sql이 자동 적용됨 (localhost:5434)

# 이 DB로 서버 띄우기 (.env의 운영 DATABASE_URL을 덮어쓴다)
DATABASE_URL="postgresql://ai:<AI_DEV_DB_PASSWORD>@127.0.0.1:5434/inote_ai_dev" uvicorn app.main:app --reload --port 8000

# schema.sql을 고쳤다면 이미 만든 DB에 다시 적용 (모든 문장이 if not exists라 여러 번 실행해도 안전)
docker compose -f docker-compose.dev.yml --env-file .env.dev exec -T db psql -U ai -d inote_ai_dev -v ON_ERROR_STOP=1 < schema.sql

# post_embedding이 설계대로 동작하는지 자체 검증 (끝에서 ROLLBACK, 데이터 안 남음)
docker compose -f docker-compose.dev.yml --env-file .env.dev exec -T db psql -U ai -d inote_ai_dev -v ON_ERROR_STOP=1 < scripts/verify_post_embedding.sql
```
DB를 처음부터 다시 만들려면 `down -v`. 로컬 DB는 Supabase처럼 `extensions` 스키마에 `vector`를 설치하고 `search_path`에 `extensions`를 넣어 둔다(`docker/dev-db-init.sql`).

## 환경변수

| 변수 | 설명 |
|------|------|
| `GROQ_API_KEY` | Groq API 키 |
| `DATABASE_URL` | 이 레포 전용 Neon PostgreSQL 연결 문자열 |
| `INTERNAL_SECRET` | `inote-server`와 동일한 값이어야 함 (서비스 간 인증) |
| `INOTE_SERVER_URL` | `inote-server`의 `/api/v1` 베이스 URL (글 작성자 확인용) |
| `ALLOWED_ORIGINS` | CORS 허용 origin, 콤마로 여러 개 구분 (FE 도메인) |

## DB 스키마

`schema.sql` 참고 — `conversation_sessions`(세션) + `conversations`(메시지, `session_id` FK) + `post_embedding`(글 임베딩 벡터, pgvector).

`post_embedding`은 **벡터만** 저장한다 — 글의 공개 여부·작성자·카테고리·본문은 복사해 두지 않는다(공개 범위는 `inote-server`가 후보 글 id로 정해 넘긴다). 설계는 `inote` 레포 `docs/kure/PLAN.md` 참고.

## 배포

Render 무료 티어, `render.yaml` 블루프린트 사용. Render 대시보드에서 "New +" → "Blueprint"로
이 레포를 연결하면 `render.yaml`대로 서비스가 만들어지고, 위 환경변수 5개만 대시보드에서
직접 입력하면 된다 (전부 `sync: false`라 레포에는 값이 없음).

배포 URL: https://inote-ai.onrender.com

**콜드 스타트 방지 (TODO)** — `inote-server`는 [cron-job.org](https://cron-job.org)에서
10분마다 헬스체크를 호출해 Render 무료 플랜의 슬립(15분 무활동)을 막고 있다. `inote-ai`도
같은 방식으로 cron-job.org에 아래 값으로 등록해야 한다 (cron-job.org 계정 접근이 필요해
사람이 직접 등록):

| 항목 | 값 |
|------|-----|
| URL | `https://inote-ai.onrender.com/health` |
| Method | GET |
| 주기 | 10분마다 |

## 관련 문서

- [CLAUDE.md](./CLAUDE.md) — 로드맵/현재 체크리스트
- [DEV_LOG.md](./DEV_LOG.md) — 세션별 상세 개발 기록
