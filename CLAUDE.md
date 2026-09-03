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

## 현재 상태 (2026-09-01 기준)

레포만 생성, 코드 없음. devlog-llm(`apps/api`)의 구조·패턴을 최대한 재사용해서 세팅할 예정.

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

## 다음 할 일

- [ ] 별도 Neon DB 프로비저닝 (`inote-server`와 분리)
- [ ] FastAPI 프로젝트 세팅 (devlog-llm `apps/api` 구조 참고)
- [ ] Groq API 키 발급 또는 기존 키 재사용 여부 결정
- [ ] `inote-server`의 blog API를 호출하는 방식 설계 (RAG 인덱싱용)
- [ ] `user_id`/이메일을 요청에 태그하는 방식 확정
