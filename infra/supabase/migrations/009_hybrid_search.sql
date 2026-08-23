-- 009_hybrid_search.sql
-- Day 11.5: hybrid search — Postgres full-text search fused with the existing
-- vector search, so a chunk that matches on exact wording (not just meaning)
-- can outrank one that only sounds similar.

-- ---------------------------------------------------------------------
-- Full-text search column + index
-- ---------------------------------------------------------------------
--
-- `generated always as ... stored` keeps this column in sync with `content`
-- automatically — there is no ingestion-time code to update, and no way for
-- the two to drift, because Postgres recomputes it on every insert/update of
-- `content` itself.
--
-- Image chunks' `content` is a short placeholder label ("[Image from page
-- 4]", see `ingestion.py`), not real prose. `to_tsvector` on a label like
-- that just produces a near-empty tsvector, which is harmless: it will almost
-- never match a real question and simply falls back to the vector-search half
-- of the fusion below, same as it does today.

alter table chunks
  add column content_tsv tsvector generated always as (to_tsvector('english', content)) stored;

create index chunks_content_tsv_idx on chunks using gin (content_tsv);

-- ---------------------------------------------------------------------
-- Hybrid search (vector + full-text, fused by Reciprocal Rank Fusion)
-- ---------------------------------------------------------------------
--
-- Dense (vector) search and sparse (keyword) search fail on different
-- questions: vector search finds paraphrases that share no words with the
-- question; keyword search finds exact identifiers, names, or error codes
-- that a paraphrase-trained embedding can bury under semantically-similar
-- noise. Reciprocal Rank Fusion combines the two RANKS, not the two raw
-- scores — cosine similarity and `ts_rank` live on incomparable scales, so
-- fusing the numbers directly would let whichever scale happens to be larger
-- dominate regardless of which ranking is actually better. `1 / (60 + rank)`
-- is the standard RRF constant (Cormack et al.); the exact constant matters
-- far less than fusing by rank at all.
--
-- Same security shape as `match_chunks` (005): `security invoker` (the
-- default — left unstated on purpose, same as 005), so `chunks_isolation` and
-- `documents_isolation` still scope every search to the caller. No user id is
-- threaded through this function's body for the same reason.
--
-- `similarity` in the final result is always the raw cosine similarity
-- against `query_embedding`, computed directly rather than carried over from
-- `vector_ranked` — a chunk that only the text ranker found still needs a
-- real similarity number, since Day 11.5's abstention threshold and the
-- citation display both read this field regardless of which ranker surfaced
-- the chunk.

create function public.match_chunks_hybrid(
  query_embedding vector(1536),
  query_text text,
  match_count int default 20
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
  with vector_ranked as (
    select
      c.id,
      row_number() over (order by c.embedding <=> query_embedding) as rank
    from public.chunks c
    where c.embedding is not null
    order by rank
    limit match_count
  ),
  text_ranked as (
    select
      c.id,
      row_number() over (
        order by ts_rank(c.content_tsv, websearch_to_tsquery('english', query_text)) desc
      ) as rank
    from public.chunks c
    where c.content_tsv @@ websearch_to_tsquery('english', query_text)
    order by rank
    limit match_count
  ),
  fused as (
    select
      coalesce(v.id, t.id) as id,
      coalesce(1.0 / (60 + v.rank), 0.0) + coalesce(1.0 / (60 + t.rank), 0.0) as rrf_score
    from vector_ranked v
    full outer join text_ranked t on t.id = v.id
  )
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
  from fused f
  join public.chunks c on c.id = f.id
  join public.documents d on d.id = c.document_id
  order by f.rrf_score desc
  limit match_count;
$$;

revoke all on function public.match_chunks_hybrid(vector(1536), text, int) from public;
grant execute on function public.match_chunks_hybrid(vector(1536), text, int) to authenticated;
