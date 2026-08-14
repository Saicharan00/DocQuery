-- 006_message_run_id.sql
-- Day 10a: remember which trace produced an answer, so it can still be rated
-- after a reload.

-- ---------------------------------------------------------------------
-- messages.run_id
-- ---------------------------------------------------------------------
--
-- The problem this solves: feedback is attached to a LangSmith run, and until
-- now the run id only ever existed in the browser's memory, arriving on the SSE
-- stream and dying with the page. Reload the conversation and the answers were
-- unratable — which is exactly backwards, because the answer you want to
-- complain about is usually the one you came back to.
--
-- Nullable, and it stays nullable permanently. Three ordinary rows have no run:
-- every `role = 'user'` row (a question is not something a pipeline produced),
-- every answer written before this migration, and every answer produced while
-- LangSmith tracing was switched off. `not null` would make the tracing vendor a
-- hard dependency of saving a message, which is precisely the coupling the rest
-- of Day 10a went out of its way to avoid.
--
-- No foreign key, for the same reason: the thing it points at lives in another
-- company's database. Postgres cannot check it and must not pretend to.

alter table public.messages
  add column run_id uuid;

comment on column public.messages.run_id is
  'LangSmith run that produced this answer. Null on user rows, on anything '
  'written before 006, and whenever tracing was off.';

-- The feedback endpoint's only query is "is there a message with this run id
-- that the caller is allowed to see" — RLS supplies the second half, this index
-- supplies the first. Partial, because the column is null on every user row and
-- on all history predating this migration; indexing those would roughly double
-- the index for entries no lookup can ever match.
create index messages_run_id_idx
  on public.messages (run_id)
  where run_id is not null;

-- No RLS changes. `messages_isolation` from 001_init.sql already scopes this
-- table by `user_id`, and a new column on an existing table inherits every
-- policy on it — which is the whole reason the ownership check can be a plain
-- select with no `where user_id = ...` in it.
