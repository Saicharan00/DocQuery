-- 002_grants.sql
-- Day 1 follow-up: RLS policies filter rows, but Postgres still requires a
-- plain GRANT before a role can touch a table at all. Tables created via
-- raw SQL migrations don't get Supabase's automatic anon/authenticated/
-- service_role grants the way dashboard-created tables do.

grant usage on schema public to authenticated, service_role;

grant select, insert, update, delete on
  public.users,
  public.documents,
  public.chunks,
  public.conversations,
  public.messages
to authenticated, service_role;
