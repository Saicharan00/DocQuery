# Day 1 — Supabase Foundation

Personal learning log. Not read by Claude automatically — this is for me, to
recall what I built and why once the project is done.

---

## 1. Claude Code hook: `protect-env.sh`

**What it is:** A `PreToolUse` hook (`.claude/hooks/protect-env.sh`, wired up
in `.claude/settings.json`) that runs before every `Read`, `Edit`, `Write`,
`Grep`, `Glob`, or `Bash` tool call. It inspects the tool call's JSON input
(file path, command string, glob pattern, etc.) and denies the call if it
references a real secrets file — anything matching `.env*` except the
explicitly-safe placeholder templates (`.env.example`, `.env.sample`,
`.env.template`).

**Why I built it:** I'm working with an AI assistant that has real file
system and shell access. Telling it in `CLAUDE.md` "never read `.env`" is
just a prompt-level instruction — it can be forgotten, overridden by a badly
phrased request, or slip through in a multi-step task. A hook is enforced by
the harness itself, outside the model's control, so it's a hard technical
guarantee instead of a hope.

**What it achieves:**
- Real API keys (OpenAI, Anthropic, Clerk, Supabase service role, etc.) can
  never enter the conversation transcript, get echoed back to me, or get
  logged — even if I (or a future prompt injection from some pasted content)
  accidentally ask for it.
- The backend config docs (`.env.example`) stay fully readable so Claude
  always knows *which* variables exist without ever seeing *values*.
- Verified working on Day 1: direct attempts to read `.env` are denied with
  a clear reason instead of silently failing or leaking content.

---

## 2. Supabase project setup

**Steps taken:**
1. Created a Supabase project (Postgres + pgvector + Storage all bundled).
2. Enabled the `pgvector` extension (`create extension if not exists vector;`)
   — this is what lets Postgres store and index embedding vectors directly,
   instead of running a separate vector database (Pinecone, Weaviate, etc.).
3. Scaffolded the repo layout (`apps/web`, `apps/api`, `infra/supabase/migrations`,
   `scripts`) per the plan in `BUILD.md`.

**Why pgvector instead of a dedicated vector DB:** one fewer moving part /
service to pay for and operate, and since Postgres is already the system of
record for users/documents/conversations, retrieval can join vector search
against relational data (e.g. filter by `user_id`) in a single query instead
of round-tripping between two databases.

---

## 3. Schema — `001_init.sql`

Five tables:

| Table | Purpose | Notable choice |
|---|---|---|
| `users` | Clerk user record | `clerk_id text` — Clerk IDs are strings like `user_2xY...`, not UUIDs |
| `documents` | Uploaded files | `status` check constraint: `pending / processing / ready / failed` |
| `chunks` | Parsed + embedded text pieces | `embedding vector(1536)` sized for OpenAI `text-embedding-3-small` |
| `conversations` | Chat sessions | — |
| `messages` | Individual chat turns | `user_id` **denormalized directly on the row** instead of requiring a join through `conversations` |

**Why denormalize `user_id` on `messages`:** RLS policies are evaluated per
row, independently, without implicit joins. Doing `messages.user_id = (select
user_id from conversations where id = messages.conversation_id)` inside a
policy is possible but adds a subquery to every single row check and is
easy to get subtly wrong. Storing `user_id` directly on `messages` makes the
policy identical in shape to every other table and keeps RLS cheap.

**Indexes:**
- `chunks_embedding_hnsw_idx` — HNSW index on `chunks.embedding` using
  `vector_cosine_ops`. HNSW gives approximate nearest-neighbor search, which
  is what RAG retrieval needs (semantic "closest chunks" search) and scales
  far better than an exact brute-force scan once there are many chunks.
- Plain b-tree indexes on every `user_id` column and FK column, since almost
  every query will filter by user or join by document/conversation.

---

## 4. Row Level Security (RLS)

**What I did:** Enabled RLS on every table that carries a `user_id` (or
`clerk_id` for `users`), with one consistent policy shape:

```sql
alter table <table> enable row level security;
create policy <table>_isolation on <table>
  using (user_id = (current_setting('request.jwt.claims', true)::json ->> 'sub'));
```

This reads the `sub` claim (the user's Clerk ID) out of the JWT that
PostgREST attaches to the request, and only allows a row through if it
matches. Storage got the equivalent treatment: a policy on `storage.objects`
that checks the first path segment (`{user_id}/...`) against the same claim.

**Why RLS instead of backend `WHERE user_id = X` filtering:** application
code can have bugs — a forgotten `WHERE` clause, a wrong variable, a copy-
pasted query. RLS makes the database itself refuse to return or modify rows
that don't belong to the requesting user, regardless of what the backend
code does or forgets to do. It's a single, auditable security boundary
instead of trusting every endpoint to remember to filter correctly.

**Gotcha discovered while testing (important lesson):** RLS policies only
control *which rows* a role can see — they don't grant that role permission
to touch the table *at all*. Postgres has two independent permission layers:

1. `GRANT` — can this role run `SELECT`/`INSERT`/etc. on this table at all?
2. `RLS policy` — of the rows it's allowed to touch, which ones does it see?

Tables created via raw SQL migrations (like mine) don't automatically get
the `anon`/`authenticated`/`service_role` grants that tables created through
the Supabase dashboard's Table Editor get for free. First RLS test attempt
failed with:

```
ERROR: 42501: permission denied for table documents
```

Fixed with a second migration, `002_grants.sql`:

```sql
grant usage on schema public to authenticated, service_role;
grant select, insert, update, delete on
  public.users, public.documents, public.chunks,
  public.conversations, public.messages
to authenticated, service_role;
```

**Another gotcha while verifying:** the Supabase SQL Editor runs queries as
the `postgres` superuser by default, and superusers/table owners bypass RLS
entirely (unless `FORCE ROW LEVEL SECURITY` is set). Testing RLS "as if" it
were a real API request required explicitly dropping role first:

```sql
set local role authenticated;
set local request.jwt.claims = '{"sub": "user_test_A"}';
select user_id, name from documents;  -- only returns user_test_A's row
```

Without `set local role authenticated`, every query "succeeds" and returns
all rows regardless of the claims set — which would have looked like a
passing test while actually proving nothing.

---

## 5. Verification performed (Day 1 "done when")

- [x] `001_init.sql` ran cleanly on the live Supabase project.
- [x] Inserted two fake `documents` rows under different `user_id`s, queried
      as `authenticated` with different `request.jwt.claims`, confirmed each
      simulated user only ever saw their own row.
- [x] Confirmed `pgvector` extension is enabled and the HNSW index exists on
      `chunks.embedding`.
- [x] Confirmed the `documents` Storage bucket exists (`public = false`) with
      the path-prefix isolation policy on `storage.objects`.

**What this achieves overall:** the database layer now fully enforces
per-user data isolation — for the relational tables, the vector-searchable
chunks, and the raw files in Storage — before a single line of application
code exists. Day 2 (Clerk auth) and Day 3 (FastAPI backend) can build on top
of this without RLS being an afterthought bolted on later.
