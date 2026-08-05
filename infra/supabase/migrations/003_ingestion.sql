-- 003_ingestion.sql
-- Day 6: what ingestion needs to write, and what makes a resumed step safe.

-- A document that fails has to be able to say why. Without this the UI can only
-- render the word "failed" and you are left reading Railway logs.
alter table documents add column error text;

-- The three image columns land now, on Day 6a, even though nothing writes them
-- until 6b. `chunks` is empty today, so adding them is instant. Adding them once
-- 6a has filled the table would mean re-ingesting every document and paying for
-- every embedding a second time.
--
-- Text and images share one table and one vector column on purpose: Cohere
-- embed-v4 puts both in the same 1536-dimension space, so a single similarity
-- search returns an image only when it is genuinely the best match.
alter table chunks add column chunk_type text not null default 'text'
  check (chunk_type in ('text', 'image'));
alter table chunks add column image_path text;
alter table chunks add column page_number int;

-- Ingestion runs as a sequence of separate HTTP requests, each resuming from
-- max(chunk_index). A retried or duplicated step must not be able to write the
-- same chunk twice. This is the guard: a duplicate becomes a loud database error
-- instead of a silently doubled row. Enforcing it here rather than in Python
-- means it holds no matter which code path does the insert.
create unique index chunks_document_chunk_idx on chunks (document_id, chunk_index);
