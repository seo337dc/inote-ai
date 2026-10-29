# inote-ai

`inote`(구 inote-blog)의 LLM/AI 전용 백엔드 서비스. 채팅, 대화 기록 저장, 저장 시 AI 자동 요약을
담당하며 로그인 로직은 없음 — FE가 이미 인증된 `user_id`를 실어 보내면 그대로 신뢰해서 데이터에
태깅만 한다.

왜 별도 서비스로 분리했는지, 전체 아키텍처는 `inote` 레포의 `PLANNING.md` 참고.
전체 개발 이력은 [`docs/devlog/ai.md`](./docs/devlog/ai.md), 남은 로드맵은 [`CLAUDE.md`](./CLAUDE.md) 참고.

## 아키텍처

```
inote (FE, Next.js)
  ├─ inote-server (BE, NestJS)         — 글 CRUD, 인증, 자체 Supabase DB
  └─ inote-ai (이 레포, FastAPI/Python)  — LLM 채팅, 대화 저장, AI 요약, 별도 Supabase 프로젝트(DB)
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
| POST | `/posts-chat/stream` | **글 목록 화면 채팅(글 찾기)** — 공개 글을 먼저 검색하고 찾은 글을 근거로 답변 (SSE, 요청에 `user_id`+`session_id`가 있으면 `kind="posts"`로 저장) |
| GET | `/conversations/{session_id}` | 세션의 대화 이력 조회 (`user_id` 필수) |
| GET | `/sessions` | 세션 목록 (`user_id` 필수, `q`로 검색) |
| POST | `/summarize` | 글 내용 → 불릿 요약 (내부 전용, `x-internal-secret`) |
| PUT | `/embeddings/posts/{post_id}` | 글 제목·HTML 본문 → KURE 벡터를 만들어 저장 (내용·모델이 같으면 다시 계산 안 함, 내부 전용) |
| DELETE | `/embeddings/posts/{post_id}` | 글 벡터 삭제 (없어도 오류 아님, 내부 전용) |
| POST | `/embeddings/similar` | **BE가 넘긴 후보 글 id 안에서만** 가까운 글 조회 — 저장된 글(`postId`) 또는 저장 전 텍스트(`title`+`content`) 기준 (내부 전용) |
| GET | `/health` | 헬스 체크 |

FastAPI가 기본 제공하는 Swagger UI를 그대로 씀 — 로컬 `http://localhost:8000/docs`, 배포
`https://inote-ai-cyan.vercel.app/docs` (OpenAPI 스키마는 `/openapi.json`).

## 스택

- Python + FastAPI + uvicorn
- PostgreSQL (Supabase, `inote-server`와는 별도 프로젝트) — `psycopg`
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

### 글 임베딩 API (`/embeddings/*`)

`inote-server`가 글 저장·삭제 때 부르고(실패해도 글 저장은 막지 않는다), 유사 글을 찾을 때는 **자기가 정한 "볼 수 있는 글 id 목록(`candidateIds`)"을 넘긴다.**
이 서비스는 글의 공개 여부를 모르고, **후보 안에서만** 순위를 매긴다 → 후보 밖의 글은 가까워도 나오지 않는다. 설계: `inote` 레포 `docs/kure/PLAN.md`.

```
PUT  /embeddings/posts/{id}   { "title": "...", "content": "<p>HTML 본문</p>" }
  → { "status": "embedded" | "unchanged", "model": "nlpai-lab/KURE-v1", "dim": 1024 }
POST /embeddings/similar      { "postId": "...", "candidateIds": ["..."], "limit": 5, "minScore": 0.6 }
                           또는 { "title": "...", "content": "...", "candidateIds": [...] }   (저장 전 글)
  → { "items": [{ "postId": "...", "score": 0.78 }], "sourceEmbedded": true }
```
- 임베딩 서비스(`EMBEDDER_URL`)가 없거나 응답이 없으면 임베딩이 필요한 요청은 **503** — 호출한 쪽이 기능만 숨기면 된다. 후보가 비어 있으면 임베딩 서비스를 부르지 않고 빈 결과.
- 모든 요청에 `x-internal-secret` 필요. 서버에 `INTERNAL_SECRET`이 없으면 전부 503으로 거절한다.
- 한도: 제목 500자, 본문 200만자, 후보 id 5000개, `limit` 1~50, `minScore` 0~1.

### 글 목록 채팅 `글 찾기` (`/posts-chat/stream`)

설계: `inote` 레포 `docs/kure/PLAN.md` 6절 D의 1차 범위(결정 17). 글 목록(posts) 화면의 채팅은 **검색을 먼저** 한다.

```
POST /posts-chat/stream   { "messages": [{"role": "user", "content": "도커 볼륨은 뭐고 왜 쓰나요?"}], "user_id"?: "...", "session_id"?: "..." }
  → SSE   data: {"text": "..."}                                      (답변 조각, 여러 번)
          data: {"sources": [{id,title,category,score}], "mode": "posts"|"general", "search": "found"|"none"|"unavailable"}
          data: [DONE]
```

- 흐름: 질문을 KURE로 벡터로 바꿔 **공개 글**에서 검색 → 기준 점수 이상이면 그 글만 근거로 Groq가 추천·설명(`mode: posts`) → 없거나 검색을 못 하면 "내 글에는 관련 내용이 없어요"를 먼저 알리고 일반 답변(`mode: general`, 웹 조회는 2차).
- **공개 글만**: 후보 글 id는 `inote-server`의 `GET /blog/posts/public-ids`(내부 비밀키)에서 받고, 글 내용도 BE의 공개 조회 API로 가져온다(비공개·임시저장이면 404 → 건너뜀). 이 서비스는 공개 여부를 판단하지 않는다.
- **링크는 모델이 쓰지 않는다**: `sources`의 글 id로 FE가 주소를 만들고, 답변의 `[1][2]`는 `sources` 순서를 가리킨다.
- 요청에 `system` 역할 메시지는 받지 않는다(프롬프트 끼워 넣기 방지). 질문 1000자, 대화는 최근 10개만 쓴다. 로그에는 질문 내용을 남기지 않는다.
- 1차 제약: 대화 기록을 저장하지 않고(`user_id`는 받기만 함), KURE를 질문마다 부르므로 **운영에서 열려면 KURE 호스팅이 선행**된다.

### 테스트

```bash
venv/bin/python -m unittest discover -s tests -t .        # 모델·DB 없이 도는 테스트 (DB 통합 테스트는 건너뜀)

# 실제 pgvector DB로 후보 id 제한 등을 확인하는 통합 테스트 (로컬 개발 DB, 운영 DB로 돌리지 않는다)
TEST_DATABASE_URL="postgresql://ai:<AI_DEV_DB_PASSWORD>@127.0.0.1:5434/inote_ai_dev" venv/bin/python -m unittest discover -s tests -t .
```

## 환경변수

| 변수 | 설명 |
|------|------|
| `GROQ_API_KEY` | Groq API 키 |
| `DATABASE_URL` | 이 레포 전용 Supabase 프로젝트의 PostgreSQL 연결 문자열 (Transaction pooler 6543) |
| `KAKAO_REST_API_KEY` | 독서 기록의 책 검색(카카오 도서 API)용 |
| `INTERNAL_SECRET` | `inote-server`와 동일한 값이어야 함 (서비스 간 인증) |
| `INOTE_SERVER_URL` | `inote-server`의 `/api/v1` 베이스 URL (글 작성자 확인용) |
| `ALLOWED_ORIGINS` | CORS 허용 origin, 콤마로 여러 개 구분 (FE 도메인). 쉼표 뒤 공백·값을 감싼 따옴표·끝의 `/`는 자동으로 정리하고 중복·빈 항목은 버린다(`app/cors_origins.py`). 없으면 `http://localhost:3011`만, 있는데 쓸 항목이 없으면 아무 origin도 허용하지 않음 |
| `EMBEDDER_URL` | 임베딩 서비스(`inote-embedder`) 주소. 없으면 임베딩 API는 503 (글 저장은 영향 없음) |
| `EMBEDDER_SECRET` | 임베딩 서비스가 기대하는 비밀키 — `INTERNAL_SECRET`과는 **다른 값** |
| `POSTS_CHAT_MIN_SCORE` | 글 목록 채팅의 검색 기준 점수(0~1, 기본 0.5 — A7 평가로 확정할 임시값). 1등이 이보다 낮으면 "관련 글 없음" |
| `POSTS_CHAT_SCORE_MARGIN` | 기준에 못 미쳐도 1등 점수와 이 값 이내면 함께 보여줌(0~1, 기본 0.09, 0이면 끔 — 임시값). 같은 주제 글의 점수 차이를 보완 |
| `EMBEDDER_MODEL` | 기대하는 모델 이름(기본 `nlpai-lab/KURE-v1`) — 저장된 벡터와 다르면 다시 계산 |

## DB 스키마

`schema.sql` 참고 — `conversation_sessions`(세션) + `conversations`(메시지, `session_id` FK) + `post_embedding`(글 임베딩 벡터, pgvector).

`post_embedding`은 **벡터만** 저장한다 — 글의 공개 여부·작성자·카테고리·본문은 복사해 두지 않는다(공개 범위는 `inote-server`가 후보 글 id로 정해 넘긴다). 설계는 `inote` 레포 `docs/kure/PLAN.md` 참고.

## 배포

**Vercel** (Python 함수, Framework는 FastAPI로 자동 인식). 진입점은 `app/main.py`의 `app`이라 `vercel.json`은 없고, Python 버전은 `.python-version`(3.12)으로 고정한다. 환경변수(위 표)는 Vercel 대시보드에 직접 입력하고 레포에는 값이 없다.

배포 URL: https://inote-ai-cyan.vercel.app (헬스 체크 `/health`, 진단용 `/health/db`는 내부 시크릿 헤더 필요)

- `.vercelignore`가 `venv/`, `.env`, `docs/`, 테스트 등을 번들에서 뺀다(Python 함수는 프로젝트 파일을 전부 번들에 넣는다).
- Render에서 2026-09-30에 이전했다 — 경위와 한도·트러블슈팅은 [`docs/adr/0001-render-to-vercel.md`](./docs/adr/0001-render-to-vercel.md). 서버리스라 상시 프로세스·WebSocket·백그라운드 작업이 없고, 별도의 콜드 스타트 방지 핑도 필요 없다.
- `render.yaml`·`runtime.txt`는 Render 시절 파일이 남은 것이다.
- DB: [`docs/adr/0002-neon-to-supabase.md`](./docs/adr/0002-neon-to-supabase.md)

## 관련 문서

- [CLAUDE.md](./CLAUDE.md) — 로드맵/현재 체크리스트
- [docs/devlog/ai.md](./docs/devlog/ai.md) — 세션별 상세 개발 기록
