-- 로컬 개발 DB를 Supabase와 같은 모양으로 맞춘다 (schema.sql보다 먼저 실행됨).
-- Supabase는 확장을 extensions 스키마에 설치하고, 접속 계정(postgres)의 search_path에 extensions가 들어 있다.
-- 로컬에서도 같게 해 두어야 앱 쿼리에서 vector 타입과 <=> 연산자를 스키마 이름 없이 쓸 수 있다.
create schema if not exists extensions;
alter database inote_ai_dev set search_path = "$user", public, extensions;
