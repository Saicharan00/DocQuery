-- 007_product_feedback.sql
-- What does the reader think of the whole thing, not of one answer.

-- ---------------------------------------------------------------------
-- product_feedback
-- ---------------------------------------------------------------------
--
-- Day 10a already added per-answer feedback, and that goes onto the LangSmith
-- trace of the answer it judges — the right home, because a complaint is only
-- actionable next to the retrieval and the prompt that caused it.
--
-- This is the other question, and it has no such home. "Is DocQuery any good?"
-- is not about a run, so there is no run to attach it to. LangSmith scores
-- belong to runs; filing a product verdict on whichever answer happened to be
-- last would weld it to something unrelated and quietly corrupt that answer's
-- score. So it lands in Postgres, where it can be read back and counted.

create table public.product_feedback (
  id uuid primary key default gen_random_uuid(),
  user_id text not null,

  -- 1-5 stars. Nullable because the two halves arrive as separate submissions:
  -- the star is sent the moment it is clicked, and any comment afterwards as a
  -- row of its own. Requiring both would trade the signal most people give for
  -- the one most people won't.
  rating smallint check (rating between 1 and 5),

  comment text,
  created_at timestamptz not null default now()
);

-- A row saying nothing is not an opinion, it is a bug that got as far as the
-- database. The API rejects it first; this is the backstop that means a future
-- caller cannot get one in either.
alter table public.product_feedback
  add constraint product_feedback_not_empty
  check (rating is not null or comment is not null);

comment on table public.product_feedback is
  'Feedback about DocQuery as a whole. Per-answer feedback lives on the '
  'LangSmith trace instead, since that one is about a specific run.';

create index product_feedback_user_id_idx on public.product_feedback (user_id);

-- ---------------------------------------------------------------------
-- Row Level Security
-- ---------------------------------------------------------------------
--
-- Same shape as every other table in 001. Note there is no separate `with
-- check` clause: a policy written as `using (...)` with no `for` clause covers
-- ALL commands, and for INSERT Postgres reuses the `using` expression as the
-- check. So this both hides other people's rows and refuses an insert that
-- claims someone else's user_id.

alter table public.product_feedback enable row level security;
create policy product_feedback_isolation on public.product_feedback
  using (user_id = (current_setting('request.jwt.claims', true)::json ->> 'sub'));

-- RLS filters rows; it does not grant access to the table in the first place.
-- Tables made by raw SQL miss the grants Supabase adds to dashboard-created
-- ones, which is the whole reason 002_grants.sql exists.
grant select, insert, update, delete on public.product_feedback
to authenticated, service_role;
