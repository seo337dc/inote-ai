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
