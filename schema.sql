create table if not exists conversations (
  id uuid primary key default gen_random_uuid(),
  post_id text not null,
  user_id text not null,
  role text not null,
  content text not null,
  created_at timestamptz not null default now()
);

create index if not exists conversations_post_id_idx on conversations (post_id);

comment on table conversations is '글(post)마다 나눈 LLM 대화 원문. post_id 기준으로 조회/삭제.';
comment on column conversations.post_id is 'inote-server Post.id (cuid 문자열, FK 아님 — DB가 분리돼 있어 참조 불가)';
comment on column conversations.user_id is '작성자 user_id (inote-server 세션에서 받은 값을 그대로 신뢰)';
comment on column conversations.role is 'user 또는 assistant';
comment on column conversations.content is '메시지 본문';
