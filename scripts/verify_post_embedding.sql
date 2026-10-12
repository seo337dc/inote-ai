-- post_embedding 테이블이 설계대로 동작하는지 확인하는 자체 검증 스크립트 (A4).
-- 하나라도 어긋나면 오류로 멈추고, 끝까지 가면 모두 통과한 것. 끝에서 ROLLBACK하므로 데이터는 남지 않는다.
--
-- 실행 (로컬 개발 DB):
--   docker compose -f docker-compose.dev.yml --env-file .env.dev exec -T db \
--     psql -U ai -d inote_ai_dev -v ON_ERROR_STOP=1 < scripts/verify_post_embedding.sql
--
-- 운영 AI DB(Supabase)에 적용한 뒤에도 같은 스크립트로 확인할 수 있다(SQL 편집기에서 \set 줄은 빼고 실행).

begin;

-- 1024차원 벡터를 만드는 임시 함수: a번째 칸 wa, b번째 칸 wb, 나머지 0
create function pg_temp.v(a int, wa float8, b int, wb float8) returns extensions.vector
language sql as $$
  select array_agg(case when i = a then wa when i = b then wb else 0 end order by i)::real[]::extensions.vector
  from generate_series(1, 1024) i
$$;

-- 1) 구조 ----------------------------------------------------------------------
do $$
declare t text; sch text;
begin
  -- search_path에 extensions가 있으면 타입 이름에 스키마가 안 붙으므로, 타입이 속한 스키마를 따로 확인한다
  select format_type(a.atttypid, a.atttypmod), n.nspname into t, sch
  from pg_attribute a
  join pg_type ty on ty.oid = a.atttypid
  join pg_namespace n on n.oid = ty.typnamespace
  where a.attrelid = 'post_embedding'::regclass and a.attname = 'embedding';
  assert t in ('vector(1024)', 'extensions.vector(1024)') and sch = 'extensions',
    format('embedding 칸 타입이 예상(extensions의 vector(1024))과 다릅니다: %s (스키마 %s)', t, sch);
  raise notice 'OK 1-1 embedding 타입: % (스키마 %)', t, sch;

  assert (select relrowsecurity from pg_class where oid = 'post_embedding'::regclass),
    'post_embedding에 RLS가 켜져 있지 않습니다 (프로젝트 규칙: 모든 public 테이블 RLS)';
  raise notice 'OK 1-2 RLS 켜짐';

  assert not exists (select 1 from information_schema.columns
                     where table_name = 'post_embedding'
                       and column_name in ('is_private', 'user_id', 'category', 'content', 'content_text', 'title')),
    '공개 여부·작성자·카테고리·본문을 저장하는 칸이 있으면 안 됩니다 (복사본이 어긋나면 비공개 글이 샌다)';
  raise notice 'OK 1-3 공개 여부·글 정보 복사본 칸 없음';

  assert (select count(*) from pg_constraint where conrelid = 'post_embedding'::regclass and contype = 'f') = 0,
    '다른 DB의 글을 가리키는 외래 키가 있으면 안 됩니다';
  raise notice 'OK 1-4 외래 키 없음(다른 DB의 글 id)';

  -- 앱 쿼리는 operator(extensions.<=>)로 스키마까지 붙여 쓰므로 search_path에 의존하지 않는다. 여기서는 현황만 알려 준다.
  if current_setting('search_path') like '%extensions%' then
    raise notice 'OK 1-5 search_path에 extensions 있음: %', current_setting('search_path');
  else
    raise notice '참고 1-5 search_path에 extensions가 없다(앱 쿼리는 스키마를 붙여 써서 영향 없음): %', current_setting('search_path');
  end if;
end $$;

-- 2) 저장·갱신 -------------------------------------------------------------------
-- A = e1, B = e1에 가까움(0.9·e1 + 0.1·e2), C = e3(직교), D = e1과 반대 방향
insert into post_embedding (post_id, model, content_hash, embedding) values
  ('A', 'test-model', 'h-a', pg_temp.v(1, 1.0, 2, 0.0)),
  ('B', 'test-model', 'h-b', pg_temp.v(1, 0.9, 2, 0.1)),
  ('C', 'test-model', 'h-c', pg_temp.v(3, 1.0, 4, 0.0)),
  ('D', 'test-model', 'h-d', pg_temp.v(1, -1.0, 2, 0.0));

do $$
begin
  assert (select count(*) from post_embedding) = 4, '삽입된 행이 4개가 아닙니다';

  -- 같은 글을 다시 저장하면(upsert) 덮어쓰고, 같은 글이 두 줄이 되지 않는다
  insert into post_embedding (post_id, model, content_hash, embedding)
  values ('A', 'test-model-2', 'h-a2', pg_temp.v(1, 1.0, 2, 0.0))
  on conflict (post_id) do update set
    model = excluded.model, content_hash = excluded.content_hash,
    embedding = excluded.embedding, embedded_at = now();
  assert (select count(*) from post_embedding where post_id = 'A') = 1, 'upsert 뒤 같은 글이 여러 줄입니다';
  assert (select content_hash from post_embedding where post_id = 'A') = 'h-a2', 'upsert가 해시를 갱신하지 않았습니다';
  raise notice 'OK 2 저장·upsert';
end $$;

-- 3) 가까운 이웃 (코사인 거리 <=>) ------------------------------------------------
do $$
declare ranked text; d_ab float8; d_ad float8; d_ac float8;
begin
  -- 앱 쿼리와 같은 형태(operator(extensions.<=>))로 확인한다
  select string_agg(post_id, ',' order by embedding operator(extensions.<=>) pg_temp.v(1, 1.0, 2, 0.0)) into ranked from post_embedding;
  assert ranked = 'A,B,C,D', format('가까운 순서가 A,B,C,D가 아닙니다: %s', ranked);

  select (embedding operator(extensions.<=>) pg_temp.v(1, 1.0, 2, 0.0)) into d_ab from post_embedding where post_id = 'B';
  select (embedding operator(extensions.<=>) pg_temp.v(1, 1.0, 2, 0.0)) into d_ac from post_embedding where post_id = 'C';
  select (embedding operator(extensions.<=>) pg_temp.v(1, 1.0, 2, 0.0)) into d_ad from post_embedding where post_id = 'D';
  assert d_ab < 0.05, format('거의 같은 방향인데 거리가 큽니다: %s', d_ab);
  assert abs(d_ac - 1.0) < 1e-6, format('직교 벡터의 코사인 거리는 1이어야 합니다: %s', d_ac);
  assert abs(d_ad - 2.0) < 1e-6, format('반대 방향의 코사인 거리는 2여야 합니다: %s', d_ad);
  raise notice 'OK 3 가까운 순서 %, 거리 B=% C=% D=%', ranked, round(d_ab::numeric, 4), d_ac, d_ad;
end $$;

-- 4) 후보 id 제한 — 이 서비스의 핵심 규칙 ------------------------------------------
-- BE가 넘긴 후보 id 안에서만 순위를 매기고, 후보 밖의 글은 가장 가까워도 절대 나오면 안 된다
do $$
declare ranked text;
begin
  -- A(가장 가까움)를 후보에서 뺀다
  select string_agg(post_id, ',' order by embedding operator(extensions.<=>) pg_temp.v(1, 1.0, 2, 0.0)) into ranked
  from post_embedding where post_id = any (array['B', 'C']);
  assert ranked = 'B,C', format('후보 안에서의 순서가 B,C가 아닙니다: %s', ranked);

  -- 후보가 비어 있으면 아무것도 나오지 않는다 (전체 검색으로 새지 않는다)
  assert (select count(*) from post_embedding where post_id = any ('{}'::text[])) = 0,
    '후보가 비었는데 결과가 나왔습니다';

  -- 후보에 있지만 벡터가 없는 글(아직 임베딩 전)은 조용히 빠진다
  assert (select count(*) from post_embedding where post_id = any (array['B', 'NOT-EMBEDDED'])) = 1,
    '벡터가 없는 후보 id가 결과를 망쳤습니다';
  raise notice 'OK 4 후보 id 제한: 후보 밖·빈 후보·벡터 없는 후보 모두 안전';
end $$;

-- 5) 삭제 ------------------------------------------------------------------------
delete from post_embedding where post_id = 'C';
do $$
begin
  assert (select count(*) from post_embedding where post_id = 'C') = 0, '삭제가 반영되지 않았습니다';
  raise notice 'OK 5 삭제';
end $$;

-- 6) 잘못된 입력은 거절한다 -------------------------------------------------------
do $$
begin
  begin
    insert into post_embedding (post_id, model, content_hash, embedding)
    values ('X', 'm', 'h', '[1,2,3]'::extensions.vector);
    raise exception '차원이 다른 벡터가 저장되었습니다';
  exception when others then
    if sqlerrm like '%expected 1024 dimensions%' then
      raise notice 'OK 6 차원이 다른 벡터는 거절: %', sqlerrm;
    else
      raise;
    end if;
  end;
end $$;

rollback;
\echo '모든 검증 통과 (변경은 ROLLBACK됨)'
