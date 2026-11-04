create table if not exists conversation_sessions (
  id text primary key,
  user_id text not null,
  post_id text,
  post_title text,
  post_category text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists conversation_sessions_user_id_idx on conversation_sessions (user_id);

create table if not exists conversations (
  id uuid primary key default gen_random_uuid(),
  session_id text not null references conversation_sessions (id) on delete cascade,
  role text not null,
  content text not null,
  created_at timestamptz not null default now()
);

create index if not exists conversations_session_id_idx on conversations (session_id);

-- 세션 종류(화면별 채팅 구분용 뱃지) — 기존 세션은 모두 'write'. 값은 코드(app/routers/conversations.py SESSION_KINDS)에서 검증한다.
--   write 글쓰기·일반 채팅 / posts 글 목록의 글 찾기 / reading 독서 / todo 할 일
alter table conversation_sessions add column if not exists kind text not null default 'write';
-- 글 찾기 답변의 출처 글 카드를 이력에서 복원하려고 답변과 함께 저장한다([{id,title,category,score}]). 없으면 null.
alter table conversations add column if not exists sources jsonb;

comment on table conversation_sessions is
  '대화 세션 하나. post_id가 있으면 글쓰기 세션(그 글에 묶임), null이면 일반 대화 세션.';
comment on column conversation_sessions.id is
  '세션 id — 글쓰기 세션은 inote-server Post.id를 그대로 씀, 일반 세션은 FE가 만든 UUID';
comment on column conversation_sessions.post_id is 'inote-server Post.id (일반 세션이면 null)';
comment on column conversation_sessions.post_title is '글쓰기 세션의 글 제목 스냅샷 (저장할 때마다 갱신)';
comment on column conversation_sessions.post_category is '글쓰기 세션의 글 카테고리 스냅샷';

comment on table conversations is '세션 하나에서 나눈 LLM 대화 원문.';
comment on column conversations.session_id is 'conversation_sessions.id 참조';
comment on column conversations.role is 'user 또는 assistant';
comment on column conversations.content is '메시지 본문';

-- 접속 계정(postgres)은 RLS를 우회하므로 서비스 동작에는 영향이 없다.
-- 혹시 Data API 노출 설정이 바뀌어도 anon/authenticated로는 접근되지 않게 RLS를 켜 둔다.
alter table conversation_sessions enable row level security;
alter table conversations enable row level security;

-- ─────────────────────────────────────────────────────────────────────────────
-- 글 임베딩 (KURE-v1 벡터) — 설계: inote 레포 docs/kure/PLAN.md 5.3
-- 벡터만 저장한다. 글의 공개 여부·작성자·카테고리는 **복사해 두지 않는다**(동기화가 어긋나면 비공개 글이 샌다).
-- 공개 범위는 inote-server가 "후보 글 id 목록"으로 정해 넘기고, 이 서비스는 그 안에서만 순위를 매긴다.
-- ─────────────────────────────────────────────────────────────────────────────
create schema if not exists extensions;
-- Supabase 관례: 확장은 extensions 스키마에 설치한다 (Supabase는 대시보드 Database → Extensions에서도 켤 수 있다)
create extension if not exists vector with schema extensions;

create table if not exists post_embedding (
  post_id      text primary key,
  model        text not null,
  content_hash text not null,
  embedding    extensions.vector(1024) not null,
  embedded_at  timestamptz not null default now()
);
-- 글이 수십~수백 개라 인덱스 없이 전체 비교(정확)로 충분하다. 수천 개를 넘기면 HNSW 인덱스를 검토.

comment on table post_embedding is '글 하나의 임베딩 벡터. 글 본문 텍스트는 저장하지 않는다.';
comment on column post_embedding.post_id is 'inote-server Post.id — 다른 DB라 외래 키는 없다(글이 삭제돼도 자동으로 지워지지 않으니 삭제 연동·정리 필요)';
comment on column post_embedding.model is '벡터를 만든 모델 (예: nlpai-lab/KURE-v1) — 모델을 바꾸면 이전 벡터와 섞이지 않게 구분';
comment on column post_embedding.content_hash is '임베딩한 텍스트(제목+본문 앞부분)의 해시 — 같으면 다시 계산하지 않는다';
comment on column post_embedding.embedding is '정규화된 1024차원 벡터. 거리는 코사인(<=>)';

alter table post_embedding enable row level security;

-- ─────────────────────────────────────────────────────────────────────────────
-- 임베딩 작업 대기열 — 임베딩 서비스에 닿지 못한 저장 요청을 남겨 두고, 워커가 나중에 한 건씩 처리한다.
-- 글 하나당 행 하나(post_id가 키): 같은 글을 다시 저장하면 새 행이 아니라 이 행을 갱신해서 중복 임베딩을 막는다.
-- ⚠️ 위 post_embedding과 달리 **글 본문(HTML)을 잠시 저장한다**(비공개 글 포함). 처리가 끝나면 content를 비운다.
--    글이 삭제되면 대기 작업도 함께 지워야 한다(코드에서 처리).
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists embedding_job (
  post_id    text primary key,
  title      text not null,
  content    text,
  status     text not null default 'pending' check (status in ('pending', 'processing', 'done', 'failed')),
  attempts   integer not null default 0,
  last_error text,
  version    integer not null default 1,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- 워커가 "처리할 작업"을 오래된 순으로 가져온다
create index if not exists embedding_job_status_idx on embedding_job (status, updated_at);

comment on table embedding_job is '글 임베딩 작업 대기열. 글 하나에 행 하나, 처리가 끝나면 본문(content)은 비운다.';
comment on column embedding_job.post_id is 'inote-server Post.id — post_embedding과 같은 값(다른 DB라 외래 키는 없다)';
comment on column embedding_job.content is '임베딩할 글 본문(HTML). done이 되면 null로 비운다 — 비공개 글이 오래 남지 않게';
comment on column embedding_job.status is 'pending 대기 / processing 처리 중(워커가 가져감) / done 완료 / failed 재시도 횟수 초과';
comment on column embedding_job.attempts is '처리를 시도한 횟수. 정해진 횟수를 넘으면 failed';
comment on column embedding_job.version is '같은 글이 다시 저장될 때마다 +1. 처리 중에 내용이 바뀌었는지 확인해서, 오래된 내용으로 done 처리하지 않는다';
comment on column embedding_job.updated_at is '처리 중(processing) 상태가 너무 오래 지속된 작업(워커 비정상 종료)을 다시 가져가는 기준';

alter table embedding_job enable row level security;
