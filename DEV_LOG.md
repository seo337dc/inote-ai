# DEV_LOG — inote-ai

> 세션별 작업 이력. 로드맵/현재 체크리스트는 `CLAUDE.md` 참고 — 여기는 "무엇을 왜 그렇게
> 만들었는지"를 상세히 남기는 문서.

---

## 2026-09-11 — 블로그 LLM 챗 어시스턴트: 스캐폴딩부터 세션/검색까지

`inote`(Next.js 블로그)에 "글쓰기를 돕는 LLM 챗"을 붙이는 작업. 단순 챗봇이 아니라 대화 저장,
저장 시 자동 요약, 접근 제어, 세션 목록/검색까지 이어붙였다. 한 번에 다 만들지 않고 아래 순서로
하나씩 구현 → 브라우저/curl로 확인 → 다음 단계로 진행.

### 왜 서비스를 3개로 나눴나

```
inote (FE, Next.js)
  ├─ inote-server (BE, NestJS)            — 글 CRUD, 인증(Better Auth), 자체 Neon DB
  └─ inote-ai (이 레포, FastAPI/Python)     — LLM 채팅, 대화 저장, AI 요약, 별도 Neon DB
```

블로그 CRUD/인증과 "LLM이 하는 일"을 처음부터 별도 서비스로 분리했다. Python LLM 생태계를
쓰고 싶었고, AI 쪽 장애/지연이 글쓰기 자체(inote-server)에 전파되지 않게 하려는 목적. DB도
완전히 분리돼 있어서 서로의 데이터를 직접 조인하지 못하고, 필요하면 내부 전용 API로 물어본다
(예: `inote-server`의 `GET /blog/posts/:id/owner`, 고정 시크릿 헤더 `x-internal-secret`로
서비스 간 인증). 로그인 로직 자체는 이 레포에 없음 — FE가 실어 보내는 `user_id`를 그대로
신뢰(개인 프로젝트 범위에서는 충분하다고 판단, 진짜 검증이 필요해지면 그때 추가).

### 단계별 구현

**1. 최소 스캐폴딩** — FastAPI `/chat/stream` 하나만. Groq API로 SSE 스트리밍 응답만 나옴.
DB·인증·도구 없음. 기존 FE `ChatDock`을 붙여서 브라우저로 실제 대화 확인.

**2. Draft(임시글) 개념 도입** (`inote-server`) — 저장 버튼을 누르기 전부터 그 글의 대화를
그 글에 묶으려면 "저장 전 글"도 고정 ID가 있어야 했음. `Post.publishedAt`을 nullable 필드로
추가 — null이면 draft, 값이 있으면 발행된 글. `POST /blog/posts/draft`(로그인 필요)로 글쓰기
페이지 진입 즉시 빈 Post row 생성. 공개 목록(`GET /blog/posts`)은 발행된 글만 노출, 단건
조회는 draft면 작성자 본인만(그 외 404로 존재 자체를 숨김) — 이를 위해 "로그인 안 해도 통과,
했으면 request.user 채움" 동작의 `OptionalAuthGuard` 신설.

**3. 대화 기록 저장** — `inote-ai` 전용 Neon DB 신규 프로비저닝, `conversations` 테이블
(`post_id`/`user_id`/`role`/`content`) 추가. `/chat/stream`이 `post_id`가 있을 때만
fire-and-forget으로 저장, `GET /conversations/{post_id}`로 이력 조회. FE는 `/write?id=X`
페이지에서만 `post_id`를 실어 보내고 진입 시 이력을 복원. 새로고침해도 대화가 유지되는지,
그 외 페이지의 일반 채팅은 여전히 비영속인지 둘 다 확인.

**4. 저장 시 AI 자동 요약** — 글을 진짜 저장(발행)하면 `inote-server`가 `inote-ai`의
`POST /summarize`(Groq, 불릿 3~4개, `x-internal-secret` 헤더 검증)를 호출해 요약을 받는다.
`Post`에 필드로 얹지 않고 `PostSummary`(1:1, `onDelete: Cascade`) 별도 테이블로 분리 —
요약 실패해도 글 저장 자체는 항상 성공하도록 try/catch로 격리. 빈 내용(초기 draft) 저장은
요약 호출 스킵. 실제 글 작성 → 저장 → 상세 페이지 AI 개요 박스에 진짜 요약 표시까지 확인.

**5. 접근 제어** — 다른 사용자가 남의 글 LLM 대화 기록을 못 보게 막는 단계.
`inote-server`에 내부 전용 `GET /blog/posts/:id/owner` 신설, `inote-ai`의
`app/services/ownership.py`가 이를 호출해 `post_id`의 실제 작성자와 요청자 `user_id`를
대조. `GET /conversations/{post_id}`는 `user_id` 필수 + 불일치 시 403. 포인트는 —
**소유자가 아니어도 `/chat/stream` 자체(스트리밍 응답)는 그대로 동작하되, 대화 저장만
건너뜀** ("채팅은 되지만 그 글에는 안 묶임", 기능은 막지 않고 데이터만 격리). curl로
owner/other 두 사용자 시나리오 전부 검증(조회 403, 채팅 정상 응답, other 메시지는 저장 안 됨).

**3.5. 대화 세션 목록 + 검색** — "글 하나 = 대화 하나"였던 3번 구조를, 실제 LLM 서비스들처럼
여러 대화를 옆에서 골라 쓰는 구조로 확장. `conversation_sessions`(세션) + `conversations`
(메시지, `session_id` FK) 두 테이블로 재설계. 세션 id는 글쓰기 세션이면 `post_id`를 그대로,
일반 대화 세션이면 FE가 만든 UUID. `GET /sessions?user_id=X`로 세션 목록(마지막 메시지
미리보기 포함) 제공, 일반 채팅도 로그인 상태면 저장됨(글쓰기 세션과 달리 소유권 검사 불필요,
`user_id`만 신뢰). 이후 `q` 파라미터를 추가해 `post_title`/`conversations.content`를
`ILIKE`로 검색(FE는 300ms 디바운스로 호출).

### FE UI — 세션 패널은 한 번에 정착되지 않았다

왼쪽에 항상 떠 있는 사이드바 → 하단 아이콘으로 여닫는 토글 패널 → (처음엔) 채팅 영역을
통째로 대체하는 방식 → 클로드처럼 채팅 옆에 나란히 두는 side-by-side 방식 → 최종적으로
"채팅 위에 슬라이드로 겹쳐지는 오버레이"(데스크탑은 채팅 영역의 80%만 덮고 나머지 20%는
딤 처리, 모바일은 시트 전체를 덮음)로 정착. 그 밖에 입력창은 높이를 키우고 Shift+Enter로
줄바꿈/Enter로 즉시 전송하도록 개선.

카테고리 필터 영역도 처음엔 홈에 뒀다가, 돌아보니 "내 글을 관리하는" 성격이 강해 "나의 글"
페이지로 옮기고 모바일 드로어는 홈/나의 글 양쪽에서 다 열리게 정리.

### 개발 중 발견해 같이 고친 버그 3건

1. **자동저장 400 Bad Request** — `UpdatePostDto`가 `PartialType(CreatePostDto)`를 통해
   `@MinLength(1)`을 그대로 물려받아서, `publish` 없는 자동저장 요청도 제목/본문 최소 길이
   검증에 걸림. `@ValidateIf((o) => o.publish === true)`로 `publish`가 true일 때만
   검증하도록 수정.
2. **메시지 없는 새 세션 조회가 403** — `get_history`가 "세션 row 없음"과 "세션은 있는데
   소유자가 다름"을 똑같이 403으로 처리하던 버그. 전자는 빈 배열, 후자만 403으로 분리.
3. **모바일 글 상세 페이지 가로 스크롤** — 처음엔 `.prose`의 줄바꿈 규칙(`overflow-wrap`)
   문제로 보였지만, 진짜 원인은 LLM 패널을 헤더 옆에 두려고 만든 최상위 `flex` 레이아웃
   컨테이너에 `min-w-0`이 빠져 있던 것 — flex item 기본값 `min-width: auto` 때문에 내부
   콘텐츠가 뷰포트보다 넓게 밀려나는 전형적인 flex 오버플로우 버그였다.

### 아직 안 한 것 (`CLAUDE.md` 로드맵 6~8번)

- 웹 검색 도구 — Groq `compound-beta` 연동
- "에디터에 삽입" tool-call — LLM이 자유 대화 중 제안하거나 사용자 요청 시 실제로 에디터에
  글을 써넣는 기능 (고정 버튼이 아니라 LLM이 직접 tool을 호출하는 방식으로 설계)
- 오래된 draft 정리 배치 — `inote-server`에 매일 자정 크론으로 `publishedAt IS NULL` +
  2일 지난 `Post` 삭제, 삭제된 postId로 `inote-ai`를 호출해 관련 대화 기록도 같이 삭제

### README 정리 + Render 배포 준비

`inote-ai` README가 "역할/스택 (예정)" 상태로 방치돼 있던 걸(실제로는 위 내용 전부 이미
구현됨) 현재 구현 기준으로 다시 씀 — API 목록, 환경변수, 로컬 실행 방법 추가.

Render 배포를 위해 `render.yaml` 신규 추가 — `inote-server`가 쓰던 Blueprint 방식과 동일한
패턴(`runtime`/`buildCommand`/`startCommand`/`healthCheckPath`). Python 서비스라 `buildCommand`는
`pip install -r requirements.txt`, `startCommand`는 `uvicorn app.main:app --host 0.0.0.0 --port
$PORT`. 환경변수 5개(`GROQ_API_KEY`/`DATABASE_URL`/`INTERNAL_SECRET`/`INOTE_SERVER_URL`/
`ALLOWED_ORIGINS`)는 전부 `sync: false`로 — 실제 값은 레포에 안 남기고 Render 대시보드에서
직접 입력하도록. 실제 배포(서비스 생성, 값 입력)는 계정 접근이 필요해 사람이 직접 진행.

#### 🐛 첫 배포 실패 — `psycopg-binary==3.2.4` 설치 불가 (Python 버전 미스매치)

Render에서 실제로 Blueprint 배포를 시도하자 빌드 단계에서 실패:
`ERROR: Could not find a version that satisfies the requirement psycopg-binary==3.2.4`.

원인 파악: 로그에 찍힌 다른 패키지(`pydantic-core`)의 wheel 파일명이 `cp314`였다 — Render의
기본 Python 빌드 이미지가 3.14라는 뜻. `requirements.txt`에 고정한 `psycopg[binary]==3.2.4`는
PyPI 기준 cp38~cp313까지만 prebuilt wheel을 제공하고 cp314는 없음(로컬 venv는 Python 3.9라
문제없이 설치됐던 것). `runtime.txt`/`PYTHON_VERSION` 같은 버전 고정 없이 배포하면 Render가
쓰는 기본 Python 버전에 그대로 노출되는 구조라 생긴 문제.

**해결**: `runtime.txt`(`python-3.12.7`) 추가 + `render.yaml`에도 `PYTHON_VERSION=3.12.7`
env var를 명시해서 이중으로 고정. 3.12는 `psycopg-binary==3.2.4`가 지원하는 범위 안이라 이
조합으로 재배포하면 해결될 것으로 예상(로컬엔 3.12 인터프리터가 없어 완전한 재현 검증은
못 했고, 다음 배포 시도에서 확인 필요).

#### 📄 콜드 스타트 방지 — 아직 미설정, `inote-server` 방식 참고 필요

Render 무료 플랜은 15분 무활동 시 슬립. `inote-server`는 [cron-job.org](https://cron-job.org)
대시보드에서 `/api/v1/health`를 10분마다 호출하도록 등록해서 이 문제를 막고 있음(레포 안에는
설정 파일 없이 cron-job.org 자체 설정으로만 존재). `inote-ai`는 아직 실제 프로덕션 URL이
없어서(배포 시도 중) 이 설정을 못 했고, 배포 완료 후 같은 방식으로 `GET /health`를
cron-job.org에 등록해야 함 — README 배포 섹션에 TODO로 남겨둠.

#### ✅ 배포 성공 — https://inote-ai.onrender.com

`runtime.txt`/`PYTHON_VERSION` 고정 후 재배포 성공. `curl`로 `/health` 200 OK 확인.
cron-job.org 등록은 계정 접근이 필요해 아직 미완료(README 배포 섹션에 등록값 남겨둠) —
등록 전까지는 15분 무활동 시 슬립, 첫 요청에 콜드 스타트(수십 초) 발생할 수 있음.

**남은 연동 작업** (배포 URL이 나왔으니 이어서 해야 함):
- `inote-server` Render 환경변수 `INOTE_AI_URL` → `https://inote-ai.onrender.com`
- `inote`(FE) Vercel 환경변수 `NEXT_PUBLIC_AI_API_URL` → `https://inote-ai.onrender.com`
- `inote-ai`의 `ALLOWED_ORIGINS` 환경변수에 `inote` 프로덕션 도메인이 들어있는지 확인

#### ✅ Swagger 문서 확인

FastAPI는 별도 설정 없이도 `/docs`(Swagger UI)와 `/openapi.json`을 자동 생성한다 — 각
라우터에 `tags`(`chat`/`conversations`/`sessions`/`summarize`)가 이미 붙어 있어서 그룹핑도
바로 정리돼 보임. 배포된 `https://inote-ai.onrender.com/docs`에서 5개 엔드포인트(`/chat/stream`,
`/conversations/{session_id}`, `/sessions`, `/summarize`, `/health`) 전부 정상 노출되는 것
확인. README API 섹션에 Swagger URL 추가.

## 2026-09-14 — 게스트 채팅 422 버그 수정

로컬 FE/BE + 배포된 AI 도메인 조합으로 테스트하다가 발견: 로그인 안 한 게스트가 채팅을
보내면 항상 422가 났다. `ChatRequest.session_id`가 `str`(필수)인데, FE는 게스트일 때
`session_id`를 `null`로 보내도록 원래 설계돼 있어서(세션 자체가 없으니까) — 3.5단계에서
세션 구조로 확장하며 이 필드를 필수로 만들 때 게스트 경로를 놓친 것. 배포본에서도 계속
깨져 있던 상태였고, 화면엔 "답변을 가져오지 못했어요"로만 조용히 실패해서 그동안 못 잡았음.

**수정**: `session_id: Optional[str] = None`으로 변경, `can_persist`를
`data.session_id is not None`부터 시작하도록 재구성(게스트는 무조건 저장 안 함, 로그인
세션은 기존처럼 post_id 소유권 검사).

로컬 uvicorn으로 두 경로 다 curl 검증: 게스트(session_id null) → 스트리밍 정상 응답 +
DB에 아무것도 안 남음, 로그인(session_id 있음) → 스트리밍 정상 응답 + DB에 세션/메시지
정상 저장. 테스트로 만든 세션은 확인 후 삭제.
