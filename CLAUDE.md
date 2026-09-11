# CLAUDE.md — inote-ai

새 세션(다른 PC, 다른 AI 포함)에서 이 프로젝트를 이어받을 때 먼저 읽는 파일. 이 레포는
`inote-blog`의 서브 컴포넌트라서, "왜 만드는지"·"전체 아키텍처"는 **`inote-blog` 레포의
`PLANNING.md`·`STRATEGY.md`를 먼저 읽을 것**.

---

## 이 레포의 역할

`inote-blog`의 LLM/AI 전용 백엔드. 블로그 CRUD·인증은 전혀 다루지 않음 (그건 `inote-server`의
`blog` 모듈 담당). 이 레포는 오직:

- LLM 채팅 (Groq)
- 대화 기록 저장
- 임베딩 생성·저장, RAG 검색

로그인 로직 없음 — FE(`inote-blog`)가 이미 인증된 `user_id`/이메일을 요청에 실어 보내면, 그 값을
그대로 신뢰해서 데이터에 태깅만 함. 진짜 인증 검증이 필요해지면 그때 추가.

## 아키텍처

```
inote-blog (FE)
  → inote-ai (이 레포, Python/FastAPI) → Groq (LLM), 자체 DB(PostgreSQL + pgvector)
  → inote-server의 blog 모듈 (글 CRUD, 인증)

inote-ai → inote-server의 blog API 호출 (RAG 인덱싱 시 글 내용 조회용, DB 직접 조인 안 함)
```

## 현재 상태 (2026-09-11 기준)

레포만 생성, 코드 없음. devlog-llm(`apps/api`)의 구조·패턴을 최대한 재사용해서 세팅할 예정.
블로그 관련 LLM 기능만 먼저 범위를 잡아 설계를 확정함 (아래 참고) — 구현은 아직 시작 전.

## 블로그 LLM 기능 — 범위·설계 결정 (2026-09-11)

먼저 블로그(글쓰기) 관련 기능만 진행. 할일 자동화 등 다른 기능은 이후 별도 설계.

**기능 범위**

1. 공통 — 일반 LLM처럼 검색 기능 기본 탑재 (웹 검색 + 내 글 검색(RAG) 둘 다. RAG는 대화
   기록 인프라가 먼저 있어야 해서 순서상 뒤에 옴)
2. 블로그 쓰기/수정 페이지
   - LLM이 지금 쓰고 있는 글(저장 전 draft 포함)을 분석
   - LLM이 자유 대화 흐름 중 "정리해서 글에 추가해드릴까요?" 라고 먼저 제안하거나, 사용자가
     아무 때나 "내가 넣어줘"라고 요청하면 그 시점에 에디터에 실제로 삽입 — 고정 버튼이 아니라
     **LLM이 "에디터에 삽입" tool을 직접 호출**하는 방식 (Groq tool calling)
   - 저장 완료 후 `inote-server`가 저장 성공 응답을 받으면 `inote-ai`를 호출해 요약을 받고,
     `inote-server` DB의 **별도 `PostSummary` 테이블**(Post와 1:1 관계, Post 필드 확장이 아님)에 저장
   - 글(post)마다 대화 세션 전체를 유지 — 실제 LLM과 나눈 대화 원문 그대로 보존
3. 블로그 상세(다른 사용자가 보는 화면) — 그 글의 LLM 대화 기록은 작성자 외 조회 불가

**설계 결정**

- 검색: 별도 검색 API 계약 없이 Groq의 `compound-beta`(웹 검색·코드실행 내장 모델)로 웹 검색부터.
  RAG(내 글 검색)는 대화 기록 저장 인프라가 갖춰진 뒤 이어붙임.
- 새 글 작성 시작 시점에 **`inote-server`에 빈 Post row를 바로 생성**해서 고정 postId를 먼저
  확보 (draft id 별도 관리 안 함).
- 요약 저장 흐름은 **동기 호출** — `inote-server`가 저장 후 `inote-ai`를 호출해 요약을 받아
  자기 DB(`PostSummary` 테이블)에 저장. 공개 상세 페이지의 읽기 경로가 `inote-ai` 가용성에
  의존하지 않도록 하기 위함.
- `inote-ai` ↔ `inote-server` 서버 간 인증은 고정 시크릿 헤더로 시작 (내부 전용, 최소 구현).

## 로컬 개발

아직 세팅 전.

## 배포

아직 배포 전. devlog-llm과 동일하게 Render 무료 티어 예정.

## Git / GitHub 계정

`inote-blog`/devlog-llm과 동일하게 개인 계정 `seo337dc` 전용으로 로컬 git 설정 완료
(`user.name`/`user.email`, push 인증용 `credential.helper` 로컬 오버라이드).

## 참고할 것 (devlog-llm에서 이미 검증된 패턴)

- **Groq SSE 스트리밍 채팅**: `devlog-llm/apps/api/app/routers/chat.py`
- **대화 저장(fire-and-forget)**: `devlog-llm/apps/api/app/routers/conversations.py`
- **임베딩 테이블 설계안 A/B**: `devlog-llm/docs/DB_ROADMAP.md`
- **RAG 개념 정리**: `devlog-llm/docs/study/rag.md`
- **주의**: pydantic `BaseModel` 필드는 `Optional[X]` 필수 — `from __future__ import annotations` +
  `X | None`은 Python 3.9에서 런타임 에러 (devlog-llm CLAUDE.md "알아둘 것" 참고)

## 다음 할 일 — 블로그 LLM 기능 진행 순서 (2026-09-11 확정, 하나씩 확인하며 진행)

한 번에 몰아서 하지 않고, 각 단계마다 실제로 확인한 뒤 다음 단계로 넘어간다.

- [x] **1. inote-ai 최소 스캐폴딩** — FastAPI + `/chat/stream`(Groq 스트리밍만, DB·인증·도구 없음).
      기존 FE `ChatDock`을 여기에 연결해 브라우저에서 실제 대화 확인 완료 (2026-09-11).
- [x] **2. 새 글 작성 시 빈 Post row 먼저 생성** (`inote-server`) — 매 글마다 고정 postId 확보.
      `Post.publishedAt`(nullable) 추가, `/blog/posts/draft`(로그인 필요) 신설, 공개 목록은
      발행된 글만, 단건 조회는 draft면 작성자만(그 외 404). `/write`는 이제 draft를 클라이언트
      쪽에서 쿠키 포함 fetch로 가져옴(SSR이 쿠키를 못 넘겨서). 브라우저로 전체 흐름 확인 완료
      (2026-09-11).
- [x] **3. 대화 기록 저장** (`inote-ai` DB, `post_id` 기준) — 새로고침해도 대화가 유지되는지 확인.
      `inote-ai` 전용 Neon DB 신규 프로비저닝, `conversations` 테이블(post_id/user_id/role/
      content) 추가, `/chat/stream`이 post_id 있을 때만 저장(fire-and-forget), `GET
      /conversations/{post_id}`로 이력 조회. FE `useChatMessages`가 `/write?id=X`일 때만
      post_id를 실어 보내고 진입 시 이력을 복원, 그 외 페이지는 기존처럼 비영속 일반 채팅.
      브라우저로 저장→새로고침→복원, 일반 채팅 비영속 둘 다 확인 완료 (2026-09-11).
- [x] **4. 저장 후 자동 요약** — `inote-ai`↔`inote-server` 고정 시크릿 헤더 연결 + `PostSummary`
      테이블(`inote-server`) + 저장 로직. 상세 페이지의 mock `PostAiSummary.tsx`를 실제 데이터로 교체.
      `inote-ai`에 `POST /summarize`(Groq, 불릿 3~4개, `x-internal-secret` 헤더 검증) 신설.
      `inote-server`의 `update()`가 저장 성공 시 이를 호출해 `PostSummary`에 upsert (실패해도
      저장 자체는 성공 — try/catch로 격리). 빈 내용 저장(초기 draft)은 요약 호출 스킵.
      `findOne`/`findRaw`에 `aiSummary` include 추가. 브라우저로 실제 글 작성→저장→AI 개요
      박스에 진짜 요약 표시까지 확인 완료 (2026-09-11).
- [x] **5. 접근 제어 확인** — 다른 사용자가 글의 LLM 대화 기록을 못 보는지 검증.
      `inote-server`에 내부 전용 `GET /blog/posts/:id/owner`(고정 시크릿 헤더) 신설.
      `inote-ai`의 `app/services/ownership.py`가 이걸 호출해 `post_id`의 실제 작성자와
      요청자 `user_id`를 대조. `GET /conversations/{post_id}`는 이제 `user_id` 필수 +
      불일치 시 403. `/chat/stream`은 소유자가 아니어도 채팅 자체(스트리밍 응답)는 그대로
      동작하되, 대화 저장(user/assistant 메시지 모두)만 건너뜀 — "채팅은 되지만 그 글에는
      안 묶임". curl로 owner/other 두 사용자 시나리오 전부 검증(조회 403, 채팅 정상 응답,
      other의 메시지는 저장 안 됨) + 브라우저로 본인 draft 정상 동작 확인 완료 (2026-09-11).
- [x] **3.5 (추가, 2026-09-11) 대화 세션 목록 + 새 대화** — 3번의 `post_id` 단위 저장을
      `conversation_sessions`(세션) + `conversations`(메시지, `session_id` FK) 구조로 확장.
      세션 id는 글쓰기 세션이면 `post_id`를 그대로, 일반 세션이면 FE가 만든 UUID를 씀.
      `GET /sessions?user_id=X`(목록, 세션마다 마지막 메시지 미리보기 포함) 신설. 일반
      채팅도 이제 로그인 상태면 저장됨(글쓰기 세션과 달리 소유권 검사 불필요, `user_id`만
      신뢰). FE: 세션 목록은 하단 아이콘으로 토글하는 오버레이 패널(데스크탑은 채팅 영역의
      80%만 덮고 나머지 20%는 딤 처리, 모바일은 시트 전체를 덮음)로 구현 — 글쓰기 세션은
      "글쓰기·카테고리·제목", 일반은 마지막 메시지 미리보기로 구분 표시, "새 대화" 버튼,
      클릭해도 페이지 이동 없이 그 세션으로 전환만 됨(다른 방식은 추후 고민 중).
      `GET /sessions`에 `q` 파라미터 추가 — `post_title`/`conversations.content`를
      `ILIKE`로 검색, 세션 목록 상단 검색창에서 300ms 디바운스로 호출.
      **이 작업 중 발견해 같이 고친 버그 2건**: (1) 자동저장이 `UpdatePostDto`가 물려받은
      `MinLength(1)` 때문에 제목만 쓰고 본문이 비어있으면 400으로 실패하던 것 — `publish`가
      true일 때만 최소 길이를 요구하도록 `ValidateIf`로 수정. (2) 메시지를 한 번도 안 보낸
      새 세션의 히스토리 조회가 403을 내던 것 — 존재하지 않는 세션은 403이 아니라 빈 배열이
      맞음. 브라우저로 일반/글쓰기 세션 둘 다 저장·전환·실시간 제목 반영·검색까지 확인 완료.
- [ ] **6. 웹 검색 도구 추가** — Groq `compound-beta` 연동.
- [ ] **7. "에디터에 삽입" tool-call 추가** — 자유 대화형(제안 또는 사용자 요청 시 실제 삽입).
- [ ] **8. 오래된 draft 정리 배치** (3번 완료 후) — `inote-server`에 매일 00시 크론
      (`@nestjs/schedule`, `inote-server`는 이미 별도 헬스체크로 계속 깨어있어서 인프로세스
      크론으로 충분 — 슬립 걱정 없음): `publishedAt IS NULL`이고 `createdAt` 기준 2일 지난
      `Post` 삭제 → 삭제된 postId 목록을 들고 `inote-ai`를 호출(고정 시크릿 헤더)해서 그
      글들의 대화 기록도 같이 삭제.

**2026-09-11 재검토 — draft를 없앨지 고민했으나 유지로 결론**

빈 draft가 DB에 영구히 남는 게 곤란해 보여서 "저장 전엔 채팅도 로컬 메모리에만 두고, 저장
시점에 한 번에 올리는" 대안을 검토했었음. 하지만 그러면 새로고침/탭 종료 시 글 내용과 채팅
둘 다 날아가는 문제가 있어 기각. 결론: draft는 유지하고, 대신 위 8번 배치로 오래된 draft를
주기적으로 정리 — draft가 DB에 계속 쌓이는 것 자체는 "어차피 임시저장"이라 문제 아님, 무한정
쌓이지만 않으면 됨.

아래는 위 단계들 안에서 자연스럽게 같이 결정되는 것들 — 별도 단계로 안 뺌:
- 별도 Neon DB 프로비저닝 (3번 단계에서, 대화 기록 저장할 때)
- Groq API 키 발급/재사용 여부 (1번 단계에서)
- `inote-server`의 blog API 호출 방식 (4번 단계의 요약 흐름에서 필요해지면)
- `user_id`/이메일 태깅 방식 (1번 단계 `/chat` 요청부터 적용)
