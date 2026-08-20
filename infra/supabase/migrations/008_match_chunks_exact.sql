-- 008_match_chunks_exact.sql
-- Day 11: an exact-search twin of `match_chunks`, for measuring how much
-- accuracy the HNSW index's approximation actually costs.

-- `match_chunks` (005) is fast because the HNSW index lets Postgres skip most
-- of the table. "Skip most of the table" is also, by definition, a guess: HNSW
-- is an *approximate* nearest-neighbor index, and it can occasionally return
-- the 4th-closest chunk while missing the true 3rd-closest one. There is no
-- per-call flag that turns approximation off — the only way to get a true
-- answer is a query the planner cannot serve from the index at all.
--
-- `set enable_indexscan = off` is that: it forces this function's query onto a
-- sequential scan, comparing the query vector against every single chunk
-- row. Slow on purpose — this function exists to be the ground truth
-- `match_chunks` is checked against, not to be called on a real request.
--
-- Everything else is a deliberate copy of 005: same columns, same
-- `security invoker` (RLS still applies — this is still a per-user search,
-- not a way around the boundary), same grant shape.

create function public.match_chunks_exact(
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
set enable_indexscan = off
as $$
  select
    c.id,
    c.document_id,
    d.name as document_name,
    c.content,
    c.chunk_index,
    c.chunk_type,
    c.image_path,
    c.page_number,
    1 - (c.embedding <=> query_embedding) as similarity
  from public.chunks c
  join public.documents d on d.id = c.document_id
  where c.embedding is not null
  order by c.embedding <=> query_embedding
  limit match_count;
$$;

revoke all on function public.match_chunks_exact(vector(1536), int) from public;
grant execute on function public.match_chunks_exact(vector(1536), int) to authenticated;
