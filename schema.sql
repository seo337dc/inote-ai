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
