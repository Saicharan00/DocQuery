-- 001_init.sql
-- Day 1: core schema, pgvector, RLS, storage bucket policy.

create extension if not exists vector;

-- ---------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------

create table users (
  id uuid primary key default gen_random_uuid(),
  clerk_id text not null unique,
  email text,
  created_at timestamptz not null default now()
);

create table documents (
  id uuid primary key default gen_random_uuid(),
  user_id text not null,
  name text not null,
  file_path text not null,
  status text not null check (status in ('pending', 'processing', 'ready', 'failed')),
  file_size int,
  mime_type text,
  created_at timestamptz not null default now()
);

create table chunks (
  id uuid primary key default gen_random_uuid(),
  user_id text not null,
  document_id uuid not null references documents (id) on delete cascade,
  content text not null,
  embedding vector(1536),
  chunk_index int not null,
  token_count int,
  created_at timestamptz not null default now()
);

create table conversations (
  id uuid primary key default gen_random_uuid(),
  user_id text not null,
  title text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table messages (
  id uuid primary key default gen_random_uuid(),
  user_id text not null,
  conversation_id uuid not null references conversations (id) on delete cascade,
  role text not null check (role in ('user', 'assistant')),
  content text not null,
  model text,
  sources jsonb,
  created_at timestamptz not null default now()
);

-- ---------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------

create index chunks_embedding_hnsw_idx
  on chunks
  using hnsw (embedding vector_cosine_ops);

create index documents_user_id_idx on documents (user_id);
create index chunks_user_id_idx on chunks (user_id);
create index chunks_document_id_idx on chunks (document_id);
create index conversations_user_id_idx on conversations (user_id);
create index messages_user_id_idx on messages (user_id);
create index messages_conversation_id_idx on messages (conversation_id);

-- ---------------------------------------------------------------------
-- Row Level Security
-- ---------------------------------------------------------------------

alter table users enable row level security;
create policy users_isolation on users
  using (clerk_id = (current_setting('request.jwt.claims', true)::json ->> 'sub'));

alter table documents enable row level security;
create policy documents_isolation on documents
  using (user_id = (current_setting('request.jwt.claims', true)::json ->> 'sub'));

alter table chunks enable row level security;
create policy chunks_isolation on chunks
  using (user_id = (current_setting('request.jwt.claims', true)::json ->> 'sub'));

alter table conversations enable row level security;
create policy conversations_isolation on conversations
  using (user_id = (current_setting('request.jwt.claims', true)::json ->> 'sub'));

alter table messages enable row level security;
create policy messages_isolation on messages
  using (user_id = (current_setting('request.jwt.claims', true)::json ->> 'sub'));

-- ---------------------------------------------------------------------
-- Storage bucket + path-prefix RLS
-- ---------------------------------------------------------------------

insert into storage.buckets (id, name, public)
values ('documents', 'documents', false)
on conflict (id) do nothing;

create policy documents_bucket_isolation on storage.objects
  for all
  using (
    bucket_id = 'documents'
    and (storage.foldername(name))[1] = (current_setting('request.jwt.claims', true)::json ->> 'sub')
  )
  with check (
    bucket_id = 'documents'
    and (storage.foldername(name))[1] = (current_setting('request.jwt.claims', true)::json ->> 'sub')
  );
