# inote-ai

`inote-blog`의 LLM/AI 전용 백엔드 서비스. 채팅, 임베딩, RAG 검색을 담당하며 로그인 로직은 없음.

왜 별도 서비스로 분리했는지, 전체 아키텍처는 `inote-blog` 레포의 `PLANNING.md`/`STRATEGY.md` 참고.

## 역할

- LLM 채팅 (Groq)
- 대화 기록 저장
- 임베딩 생성·저장, RAG 검색

로그인은 없음 — `inote-blog`(FE)가 이미 인증된 `user_id`/이메일을 요청에 실어 보내면, 그 값을
그대로 신뢰해서 데이터에 태깅만 함.

## 스택 (예정)

- Python + FastAPI
- PostgreSQL + pgvector (`inote-server`와는 별도 DB)
- Groq API

devlog-llm(선행 실험 프로젝트)에서 검증한 패턴(Groq SSE 스트리밍, `conversations` 저장, RAG 설계안)을
최대한 재사용할 예정. 자세한 내용은 [`CLAUDE.md`](./CLAUDE.md) 참고.
