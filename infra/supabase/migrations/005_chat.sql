-- 005_chat.sql
-- Day 7: vector search, and a daily spend brake for chat.

-- ---------------------------------------------------------------------
-- Vector search
-- ---------------------------------------------------------------------
--
-- Why this has to be a function at all: every other query in this app goes
-- through PostgREST (`supabase.table("chunks").select(...)`), and PostgREST has
-- no syntax for `order by embedding <=> $1`. The `<=>` operator simply cannot be
-- expressed in its query language. So the one query that matters most gets
-- written in SQL and called with `.rpc()`.
--
-- SECURITY NOTE, and it is the opposite of 004's.
--
-- `documents_created_today()` in 004 is `security definer` on purpose: it has to
-- see past RLS to count rows belonging to everybody. This function must NOT be.
-- It is left as the default `security invoker`, so it runs with the calling
-- user's privileges and the `chunks_isolation` / `documents_isolation` policies
-- from 001 still apply inside the function body. That is the entire thing
-- stopping one user's question from searching another user's documents. Adding
-- `security definer` here would silently turn the app's central security
-- boundary off and everything would still appear to work.
--
-- For the same reason it needs no `set search_path`. That hardening exists to
-- stop a `security definer` body being tricked into reading an attacker's table
-- while holding elevated privileges. There are no elevated privileges here.
--
-- On the maths: `<=>` is pgvector's cosine *distance* operator - smaller means
-- more similar - which is why the sort is ascending and why `001`'s HNSW index
-- was built with `vector_cosine_ops`. It has to be this operator and no other,
-- or the index is ignored and every search becomes a full scan. `1 - distance`
-- converts it to a similarity, because "0.87 similar" is a number a human can
-- reason about and 0.13 distance is not. Day 11.5 sets an abstention threshold
-- against this value.

create function public.match_chunks(
  query_embedding vector(1536),
  match_count int default 5
)
returns table (
  id uuid,
  document_id uuid,
  document_name text,
  content text,
  chunk_index int,
  chunk_type text,
  image_path text,
  page_number int,
  similarity float
)
language sql
stable
as $$
  select
    c.id,
    c.document_id,
    -- Joined rather than stored on the chunk: a citation has to say which
    -- document it came from, and duplicating the name onto every chunk would
    -- go stale the moment a document is renamed. `documents` is RLS-protected
    -- too, so this join cannot leak a name across users.
    d.name as document_name,
    c.content,
    c.chunk_index,
    c.chunk_type,
    c.image_path,
    c.page_number,
    1 - (c.embedding <=> query_embedding) as similarity
  from public.chunks c
  join public.documents d on d.id = c.document_id
  -- The column is nullable. A row without a vector cannot be compared to
  -- anything, and including it would make the sort's behaviour depend on where
  -- Postgres decides to put nulls.
  where c.embedding is not null
  order by c.embedding <=> query_embedding
  limit match_count;
$$;

-- Functions are executable by everyone by default. Take that away, then grant it
-- back only to signed-in users - same pattern as 004.
revoke all on function public.match_chunks(vector(1536), int) from public;
grant execute on function public.match_chunks(vector(1536), int) to authenticated;


-- ---------------------------------------------------------------------
-- Global daily chat limit
-- ---------------------------------------------------------------------
--
-- The same problem 004 solved for uploads, now for questions: RLS scopes every
-- query to the caller, so the API physically cannot total up today's messages
-- across all users. Written as a normal query it would silently return the
-- caller's own count and stop nothing.
--
-- Every answer is billed to my own key, so this is the brake on the whole
-- service rather than on one visitor. Kept as small as 004's for the same
-- reason: no arguments, no caller-supplied table access, one integer out. A
-- caller learns how busy the service is today and nothing about whose questions
-- those were.
--
-- `set search_path` IS required here, unlike `match_chunks` above, because this
-- one really is `security definer`. Without it a caller could create their own
-- `messages` table in a schema earlier on the search path and have this read
-- that instead.
--
-- Counts `role = 'user'` only. Each user message causes exactly one LLM call;
-- the assistant row is its result, and counting both would halve the real cap
-- for no reason.

create function public.messages_created_today()
returns integer
language sql
security definer
set search_path = public, pg_temp
stable
as $$
  select count(*)::int
  from public.messages
  where role = 'user'
    and created_at >= date_trunc('day', now() at time zone 'utc');
$$;

revoke all on function public.messages_created_today() from public;
grant execute on function public.messages_created_today() to authenticated;
