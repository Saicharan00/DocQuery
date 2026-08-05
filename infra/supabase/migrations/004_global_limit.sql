-- 004_global_limit.sql
-- Day 6: a global daily kill switch, without handing the API a key that can
-- bypass RLS for writes.
--
-- The problem this solves: RLS scopes every query to the calling user, so the
-- API physically cannot total up today's uploads across all users. A "global"
-- count written as a normal query would silently return the caller's own count
-- and stop nothing.
--
-- `security definer` makes the function run with the privileges of the role that
-- created it rather than the role that calls it, so it can see past RLS. That is
-- a real escalation, so it is kept as small as it can possibly be: no arguments,
-- no table access from the caller, and a single integer comes back. A caller
-- learns how busy the service is today and nothing whatsoever about whose
-- documents those are.
--
-- `set search_path` is not optional on a security definer function. Without it a
-- caller could create their own `documents` table in a schema earlier on the
-- search path and have this function read that instead.

create function public.documents_created_today()
returns integer
language sql
security definer
set search_path = public, pg_temp
stable
as $$
  select count(*)::int
  from public.documents
  where created_at >= date_trunc('day', now() at time zone 'utc');
$$;

-- Functions are executable by everyone by default. Take that away, then grant it
-- back only to signed-in users.
revoke all on function public.documents_created_today() from public;
grant execute on function public.documents_created_today() to authenticated;
