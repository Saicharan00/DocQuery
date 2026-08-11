# BUILD.md

12-day build plan. Read `CLAUDE.md` first for stack + working agreement.

Each day has a **Goal**, **Steps**, and a **Done when** checklist. Don't skip the Done-when — if it doesn't pass, the day isn't finished.

---

## Project pitch (one line)

> Multi-model RAG chat over your documents. Upload PDFs, DOCX, or TXT. Pick your model. Answers cite their sources — with the figures they came from.

*(BYOK dropped 2026-08-03. It is a rate-limited live demo running on my own keys: per-user daily caps plus a global kill switch. Cost is a design constraint, not the visitor's problem.)*

---

## Day 1 — Supabase foundation

**Goal:** Database + vector store + storage set up with row-level security. No app code yet.

### Steps

1. Create Supabase project (region: closest to me — us-east or us-west).
2. Enable `pgvector` extension via the Supabase SQL editor.
3. Scaffold the monorepo:
   ```
   <project>/
   ├── CLAUDE.md
   ├── BUILD.md
   ├── .env.example
   ├── .gitignore
   ├── apps/web/          (empty; Day 4)
   ├── apps/api/          (empty; Day 3)
   ├── infra/supabase/migrations/
   └── scripts/           (empty; Day 11)
   ```
4. Create 5 tables in `infra/supabase/migrations/001_init.sql`:
   - `users` (id uuid PK, clerk_id text unique, email text, created_at timestamptz default now())
   - `documents` (id uuid PK, user_id text not null, name text, file_path text, status text check in ('pending','processing','ready','failed'), file_size int, mime_type text, created_at timestamptz)
   - `chunks` (id uuid PK, user_id text not null, document_id uuid FK, content text, embedding vector(1536), chunk_index int, token_count int, created_at timestamptz)
   - `conversations` (id uuid PK, user_id text not null, title text, created_at, updated_at)
   - `messages` (id uuid PK, conversation_id uuid FK, role text check in ('user','assistant'), content text, model text, sources jsonb, created_at)
   - Note: `user_id` is `text` because Clerk user IDs are strings like `user_2xY...`, not UUIDs.
5. Create HNSW index on `chunks.embedding` for cosine distance.
6. RLS policy pattern (apply to every table with `user_id`):
   ```sql
   ALTER TABLE <table> ENABLE ROW LEVEL SECURITY;
   CREATE POLICY <table>_isolation ON <table>
     USING (user_id = (current_setting('request.jwt.claims', true)::json->>'sub'));
   ```
   For `messages`, the check goes through the parent conversation's user_id (write a JOIN in the policy or duplicate user_id on messages — decide with me before implementing).
7. Create Storage bucket `documents` with path-prefix RLS: users can only read/write objects under `{user_id}/`.
8. Fill `.env.example`:
   ```
   SUPABASE_URL=
   SUPABASE_ANON_KEY=
   SUPABASE_SERVICE_ROLE_KEY=
   DATABASE_URL=
   ```

### Done when

- [ ] `001_init.sql` runs cleanly on a fresh Supabase project.
- [ ] I can insert two fake rows with different `user_id` values into `documents`, then querying as each (via `set request.jwt.claims`) returns only that user's rows.
- [ ] pgvector extension is on, HNSW index exists on `chunks.embedding`.
- [ ] Storage bucket exists with path-prefix policy.

---

## Day 2 — Clerk auth setup

**Goal:** Users can sign up. JWTs contain the user ID in the `sub` claim (which Supabase RLS reads).

### Steps

1. Create Clerk app at clerk.com. Basic sign-in/sign-up flow, email + password + Google.
2. No Organizations feature. Single-user accounts only.
3. Verify the default JWT already puts the Clerk user ID in `sub` (it does — no custom template needed for our RLS to work).
4. Note the keys:
   - `CLERK_PUBLISHABLE_KEY`
   - `CLERK_SECRET_KEY`
   - `CLERK_JWT_ISSUER` (the JWKS URL will be `<issuer>/.well-known/jwks.json`)
5. Add all three to `.env.example`.
6. Manual test: sign up in Clerk's hosted UI, copy the JWT from the session, decode at jwt.io, confirm `sub` = Clerk user ID.

### Done when

- [ ] Sign-up works in Clerk's hosted UI.
- [ ] JWT decoded shows `sub` = Clerk user ID.
- [ ] Clerk keys added to `.env.example`.

---

## Day 3 — FastAPI skeleton + Railway deploy

**Goal:** Backend exists, rejects unauthenticated requests, deployed to a live URL.

### Steps

1. Initialize `apps/api/`:
   ```
   apps/api/
     app/
       __init__.py
       main.py           # FastAPI() + CORS + router includes
       deps.py           # get_current_user, get_supabase_client
       config.py         # Settings via pydantic-settings
       routers/
         health.py       # GET /health, no auth
         me.py           # GET /me, auth required
       services/         # empty
       models/           # empty
     pyproject.toml      # uv-managed
     Dockerfile          # only if Railway needs it — try nixpacks first
   ```
2. Deps: `fastapi`, `uvicorn`, `supabase`, `python-jose[cryptography]`, `httpx`, `pydantic-settings`, `litellm`.
3. JWT middleware in `deps.py`:
   - Fetch Clerk JWKS on startup, cache in memory.
   - Verify signature (RS256) + expiry.
   - Extract `sub` (user_id).
   - Return as a FastAPI dependency: `get_current_user() -> str`.
4. Supabase client dependency:
   - Creates a per-request Supabase client.
   - Forwards the user's JWT via the `Authorization` header so PostgREST sets `request.jwt.claims` → RLS activates.
5. Endpoints:
   - `GET /health` → `{"status": "ok"}` — no auth.
   - `GET /me` → `{"user_id": "..."}` — requires auth.
6. CORS: allow the eventual Vercel origin (add localhost:3000 for dev, and a placeholder for the Vercel URL — will fill in Day 4).
7. **Deploy to Railway today.** Create the project, connect the GitHub repo, set env vars from `.env.example`. Get the live URL working.

### Done when

- [ ] `curl <railway-url>/health` → 200.
- [ ] `curl <railway-url>/me` without JWT → 401.
- [ ] `curl <railway-url>/me` with a valid Clerk JWT → 200 with user_id.
- [ ] Env vars set on Railway.

**Don't leave deployment for the end. Railway's Python cold-start quirks bite better on Day 3 than Day 12.**

---

## Day 4 — Next.js skeleton + Vercel deploy

**Goal:** Frontend exists, sign-in works, protected dashboard, talks to live backend.

### Steps

1. Initialize `apps/web/` with Next.js 15 App Router + TypeScript strict.
2. Install `@clerk/nextjs` + shadcn/ui components (button, card, input, dropdown, dialog, toast — install as needed).
3. Tailwind config, base styles.
4. Route structure:
   ```
   src/app/
     layout.tsx              # ClerkProvider
     page.tsx                # Landing
     sign-in/[[...sign-in]]/page.tsx
     sign-up/[[...sign-up]]/page.tsx
     dashboard/
       layout.tsx            # Protected via Clerk middleware
       page.tsx              # Placeholder — will hold doc list
   ```
5. Middleware protects `/dashboard/*`.
6. `src/lib/api.ts`: fetch wrapper that grabs the Clerk JWT via `useAuth().getToken()` and attaches it as `Authorization: Bearer <jwt>` on every call.
7. Dashboard page calls `GET /me` on the live Railway backend and displays the returned user_id + logged-in email.
8. **Deploy to Vercel today.** Root directory: `apps/web`. Add env vars. Once deployed, update Railway CORS to include the Vercel URL.

### Done when

- [ ] Visiting `/dashboard` signed out → redirects to sign-in.
- [ ] Sign in → land on dashboard, see user_id + email.
- [ ] On live Vercel URL, the dashboard successfully calls the live Railway `/me`.
- [ ] CORS updated on Railway to include the Vercel origin.

---

## Day 5 — Document upload

**Goal:** Upload docs to Supabase Storage, list them, delete them.

### Steps

1. Backend `apps/api/app/routers/documents.py`:
   - `POST /documents/upload` — accepts multipart file, validates (PDF/DOCX/TXT, <10MB), uploads to Storage at `{user_id}/{doc_id}.{ext}`, inserts a row in `documents` with `status='pending'`. Returns doc metadata.
   - `GET /documents` — returns all docs for the current user (RLS handles the filter).
   - `DELETE /documents/{id}` — deletes the Storage object + the DB row + any chunks (cascade — set up FK with ON DELETE CASCADE in the migration).
2. Frontend:
   - Drag-drop upload zone on `/dashboard`.
   - Doc list card showing filename, status badge, upload date, delete button.
   - After upload, refresh the list.

### Done when

- [ ] Upload a PDF on the live site → row appears in `documents` table → file exists in Storage under `{user_id}/`.
- [ ] Doc list on the frontend shows uploaded docs.
- [ ] Delete removes both the file and the DB row.
- [ ] Uploading as User A and then signing in as User B → User B sees no docs (RLS working end-to-end).

---

## Day 6 — Ingestion pipeline

**Goal:** Uploaded docs become searchable chunks with embeddings.

> **Scope changed 2026-08-03 — images are in.** This day was written text-only. It now
> also extracts images from PDFs, embeds them into the *same* vector space as the text
> (Cohere `embed-v4`), and stores them in Supabase Storage so Day 7 can return them
> beside the answer. Cost: Day 6 becomes ~1.5–2 days and it touches Day 7 (vision model)
> and Day 11 (eval questions answered only by a figure). Needs migration 003:
> `documents.error`, and `chunks.chunk_type` / `image_path` / `page_number`.
> The step list below is the old text-only version — the real one comes out of plan mode.

### Steps

1. Deps: `pymupdf`, `python-docx`, `langchain-text-splitters`, `tiktoken`. (LiteLLM already installed.)
2. `apps/api/app/services/ingestion.py`:
   - `download_from_storage(file_path) -> bytes`
   - `parse(bytes, mime_type) -> str` — dispatch to PyMuPDF / python-docx / plain read
   - `chunk(text) -> list[Chunk]` — `RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=100, length_function=<tiktoken counter>)`
   - `embed(chunks) -> list[list[float]]` — **Cohere SDK, not LiteLLM** (LiteLLM is for chat only): `cohere.ClientV2().embed(model="embed-v4.0", input_type="search_document", output_dimension=1536, embedding_types=["float"])`, batched
   - `ingest(document_id)` — orchestrates: update status → download → parse → chunk → embed → insert chunks → update status
3. **No `BackgroundTasks`.** A background job outlives its Clerk token (~60s) and RLS depends on that token. Instead the **browser drives the work in steps**: `POST /documents/{id}/ingest/step` works ~45s, writes what it finished, returns `{done, chunks_done, chunks_total}`; the browser calls again with a fresh token until `done`. Resume point is `max(chunk_index)`, so chunking must be deterministic. No service-role key, and ingestion is resumable if the tab closes.
4. Status transitions: `pending` → `processing` → `ready` | `failed`. On failure, store the error message on the `documents` row (add an `error` column via a migration).
5. Frontend: drive the step loop and show progress from its responses — **no 3s polling**, the loop already knows where it is. Status badge stays.
6. Add `COHERE_API_KEY` to `.env.example` and Railway env.

### Done when

- [ ] Upload a PDF → status flips through `pending` → `processing` → `ready` within ~30s for a small doc.
- [ ] `chunks` table has rows with 1536-dim vectors, correct `user_id` and `document_id`, sequential `chunk_index`.
- [ ] Uploading a broken file → status = `failed`, error message visible.

---

## Day 7 — RAG core

**Goal:** Chat endpoint that retrieves and generates streaming answers.

> **Scope settled 2026-08-07.** Four decisions, each with its reason:
>
> - **Single-turn only.** No conversation history in the prompt. History *and* query
>   rewriting move to Day 9 — see the note there. Deferred not for scope but because the
>   feature is worth more once Day 11 can measure the failure it fixes.
> - **Images reach the model.** A retrieved chunk with `chunk_type='image'` has its JPEG
>   pulled from Storage and sent as an image part. Its `content` is only the label
>   `[Image from page N]`, so sending text alone would tell the model nothing and waste
>   everything Day 6b built.
> - **Spend cap on `/chat`**, mirroring `_enforce_daily_limit` in `documents.py`. Chat is
>   unlimited and cheap per call, which makes it the easiest endpoint to run up a bill on.
> - **Migration 005 is unavoidable.** PostgREST has no syntax for
>   `ORDER BY embedding <=> $1`, so vector search must be a SQL function called via
>   `.rpc()`. It must **not** be `security definer` — unlike `004`, RLS is exactly what
>   we need it to keep.

### Steps

1. `apps/api/app/services/rag.py`:
   - `embed_query(question) -> vector`
   - `retrieve(user_id, query_vector, k=5) -> list[Chunk]` — cosine similarity search on `chunks`, RLS filters by user
   - `build_prompt(question, chunks) -> list[Message]` — system prompt telling the model to ground answers in provided chunks and cite sources by index; user message containing the chunks + question
2. `apps/api/app/routers/chat.py`:
   - `POST /chat` — body: `{conversation_id: str | null, message: str, model: str}`
   - If `conversation_id` is null, create a new conversation, return its ID in the first SSE event.
   - Retrieve, build prompt, call `litellm.completion(model=model, messages=prompt, stream=True)`.
   - Stream tokens via SSE (`text/event-stream`).
   - After stream ends: save the user message + assistant message to `messages` table, with retrieved chunk metadata as `sources` JSON.
3. Supported models. The two dead ones (`claude-3-5-haiku-20241022`, retired 2026-02-19;
   `gemini/gemini-2.0-flash`, shut down 2026-06-01) are replaced below. **Both verified
   against provider docs 2026-08-07** — pricing current, both vision-capable:

   | Model | $/1M in – out | Notes |
   |---|---|---|
   | `gemini/gemini-3.5-flash-lite` | 0.30 / 2.50 | **default** — I pay per question |
   | `gpt-5.4-nano` | 0.20 / 1.25 | |

   One per provider, so the LiteLLM abstraction is actually exercised rather than claimed.

   **Anthropic was cut on 2026-08-10.** `claude-haiku-4-5` is $1.00 / $5.00 — ~10× the
   default on a workload where every answer is billed to me, and a third provider proves
   nothing the second one doesn't. Vision-capable is a hard requirement, not a preference:
   Day 6b's image chunks are sent as image parts, so a text-only model (DeepSeek, cheaper
   than OpenAI but blind) cannot answer the figure-only eval question at all.

   ⚠️ **`gemini-2.5-flash-lite` was the default until 2026-08-11, when the first real call
   to it returned 404 — "no longer available to new users".** Its published shutdown date
   (16 Oct 2026) was still months away and Google's catalogue endpoint still listed it;
   access had simply been closed to API keys created after some earlier cutoff. The
   replacement is `gemini-3.5-flash-lite` at $0.30 / $2.50 — 3× input, 6× output on the
   old price, about $0.002 per question at k=5.

   **The lesson is worth more than the swap:** a model being listed in a provider's
   catalogue is not the same as that model being callable with your key. Only an actual
   request distinguishes them, which is why the build tests each provider with a
   throwaway call before wiring anything to it.

   The model list is an **allowlist enforced in the request schema**. Without it a caller
   names any model they like and spends my money on it.
4. Add `GEMINI_API_KEY` and `OPENAI_API_KEY` to `.env.example` + Railway. Both are
   **optional** in `Settings`, unlike `cohere_api_key` — the API must still boot when only
   one provider is configured, and name the missing one per request instead.

### Done when

- [ ] `curl -N` on `/chat` streams tokens live.
- [ ] Answer references content from an uploaded doc.
- [ ] `sources` in the saved message contains `{document_name, chunk_index, content_preview}` for each retrieved chunk.
- [ ] Trying to chat over another user's docs is impossible (RLS blocks retrieval).

---

## Day 8 — Chat UI + model picker

**Goal:** Real conversation in the browser with streaming and citations.

### Steps

1. Chat page `/dashboard/chat/[id]`:
   - Message list — user right, assistant left.
   - Input box + send button (Enter to send, Shift+Enter for newline).
   - SSE consumer using `fetch` with a `ReadableStream` reader.
   - Render tokens as they arrive.
   - Under each assistant message: expandable "Sources" section showing each retrieved chunk (doc name + chunk index + first ~200 chars).
2. Model picker dropdown (shadcn Select) — persists in local component state.
3. "New chat" button on the sidebar (sidebar becomes real on Day 9; for now a top-of-page button is fine).
4. Loading states: "thinking..." indicator until first token arrives, then it swaps to the streaming text.
5. Error UI: bad API key, rate limit, model unavailable — clear toast/alert messages, not raw error strings.

### Done when

- [ ] Hold a real conversation on the live site.
- [ ] Switch models mid-conversation — next message uses the new model.
- [ ] Expand citations, see the chunks.
- [ ] Kill the network mid-stream → get a clean error, not a hung UI.

---

## Day 9 — Conversation history

**Goal:** Conversations persist. Users can resume old chats.

### Steps

1. Sidebar component in `/dashboard/*` layout:
   - Lists conversations for current user, sorted by `updated_at` desc.
   - Each item: title + relative time ("2h ago").
   - Click → navigates to `/dashboard/chat/{id}` and loads messages.
   - "New chat" button at top.
2. Backend `apps/api/app/routers/conversations.py`:
   - `GET /conversations`
   - `GET /conversations/{id}/messages`
   - `PATCH /conversations/{id}` (rename — body `{title}`)
   - `DELETE /conversations/{id}` (cascades to messages)
3. Auto-title new conversations after the first user message: fire-and-forget LiteLLM call with a cheap model ("Summarize this in 4 words: <first message>"), update conversation.title.
4. Chat page: on mount, if URL has a `conversation_id`, load its messages.
5. **Conversation history + query rewriting** — deferred here from Day 7.
   - Send the last **3 turns of message text** to the model. Never re-send the chunks those
     turns retrieved: they are already baked into the answers, and re-sending them is the
     version that genuinely costs money (~+19% input with text only; several times that
     with chunks).
   - **Rewriting is the hard half.** A follow-up like *"what about the second one?"* gets
     embedded literally, and those words appear nowhere near the answer — so retrieval
     returns nothing useful while the answer still reads fluently. Fix: one cheap LLM call
     that rewrites the follow-up into a standalone question *before* embedding it.
   - Day 11 measures both, which is the whole reason this waited.

### Done when

- [ ] Close browser → reopen → click an old conversation → see full history → keep chatting.
- [ ] Rename a conversation.
- [ ] Delete a conversation.
- [ ] First user message → conversation title auto-updates within a few seconds.
- [ ] A follow-up ("what about the second one?") retrieves the right chunks, not noise.

---

## Day 10 — LangSmith + error handling polish

**Goal:** Every query traced. Errors are user-friendly.

### Steps

1. Install `langsmith`.
2. Wrap the RAG pipeline in `services/rag.py` with LangSmith `@traceable` decorators or manual `Run` objects:
   - Trace top-level: `chat_query`
   - Sub-traces: `embed_query`, `retrieve`, `build_prompt`, `llm_call`
3. Tag every trace with `user_id`, `model`, `conversation_id`.
4. Verify traces show up in the LangSmith UI.
5. Pick one clean trace, make it publicly shareable, **save that URL** — it goes in the README.
6. Env vars added to `.env.example` + Railway:
   ```
   LANGCHAIN_API_KEY=
   LANGCHAIN_TRACING_V2=true
   LANGCHAIN_PROJECT=<project-name>
   ```
7. Error handling sweep across the app:
   - LLM: invalid API key, rate limit, timeout, oversized context → user-facing messages
   - Upload: unsupported type, too large, corrupt file
   - Retrieval: no docs uploaded yet → "Upload a document first"
   - Auth: token expired → clean re-auth flow
8. No `console.error` or `print(e)` left as the only response to a failure.

### Done when

- [ ] Every chat message produces a visible LangSmith trace with the full pipeline breakdown.
- [ ] One trace is public — URL saved.
- [ ] Every error path shows the user something meaningful.

---

## Day 11 — Evaluation ⭐ (two days)

**Goal:** Prove the RAG pipeline actually works. This is the interview-gold day.

> **Expanded 2026-08-07 from 3 metrics to 11.** The original three measured only whether the
> *answer* was good. They could not tell you *why* it wasn't — was the parser mangling the
> document, was the index losing chunks, was the judge itself unreliable? Each addition below
> eliminates one suspect. Grew from 1 day to 2.

### Step 0 — the contamination rule. Read this before writing a single question.

Write every question from the **source document**, in your own words, and *only then* find
the chunk that should answer it.

The natural approach — read the chunks, write a question per chunk — reuses the chunk's own
vocabulary. Retrieval then matches for reasons that have nothing to do with how a real user
types, and your hit rate is inflated with **no way to see it happening**. Phrase questions
the way someone actually asks: abbreviated, sloppy, using a synonym instead of the
document's own term.

Free if obeyed now. Costs a full rewrite of all 18 questions if remembered later.

### Steps

1. **Test corpus** — a fresh test account, containing:
   - **3–4 Paul Graham essays** (public, unambiguous, well-known). Plain text.
   - **One technical document with both figures and identifiers** — version numbers, proper
     nouns, table labels, API names. It does double duty: it is the only source for the
     figure-only question (Day 6b put images in the index; nothing has ever tested that they
     come *out*), and the only thing in the corpus hybrid search can beat dense retrieval on.
     PG essays are pure prose — BM25 has nothing to win with there.
2. **18 Q&A pairs** by hand in `scripts/eval_qa.json`:
   - 10 straightforward retrieval questions, ground-truth chunk noted.
   - 3 multi-hop questions needing 2+ chunks.
   - 2 adversarial questions whose answer is *not* in the corpus — correct behaviour is
     "I don't see this in the provided documents."
   - **2–3 multi-turn questions** — follow-ups that only make sense after a previous turn.
     These exist to measure the Day 9 rewriting work. Expect them to fail before it.
   - **1 figure-only question**, answerable solely from an image in the technical document.
3. `scripts/eval.py` — loads the pairs, runs the pipeline by importing `services/rag.py`
   directly (it is plain functions with no FastAPI in it, precisely so this is possible),
   across all three models from Day 7. Output: `eval_results.md`.

### The 11 checks

**Upstream — is the data even good?** *Run these first. Every metric below inherits their
failures, and no amount of retrieval tuning can repair a chunk that was garbage when written.*

| # | Check | What it rules out |
|---|---|---|
| 1 | **Extraction fidelity** — read parser output *by eye* for a 2-column PDF, a table-heavy report, a scanned PDF, and a DOCX with tables. Score clean / degraded / unusable. | `_parse_pdf` uses `page.get_text()`, which reads a two-column layout *across* columns; `_parse_docx` skips tables entirely (its own comment says so). Either produces plausible-looking nonsense. |
| 2 | **ANN recall vs exact search** — same questions against HNSW and against a forced sequential scan; compare the result sets. | `chunks_embedding_hnsw_idx` is **approximate**. It returns a fast guess tuned by `hnsw.ef_search` (default 40), not the true top-5. If it's losing chunks, every metric below silently inherits the loss. |

**Retrieval**

| # | Check | What it asks |
|---|---|---|
| 3 | **Hit rate / Recall@k** | Was the ground-truth chunk in the top k? (Same number as recall when one chunk is correct; genuinely different for the multi-hop questions.) |
| 4 | **MRR** | *Where* did it land? Rank 1 scores 1.0, rank 5 scores 0.2. A chunk at rank 5 is fighting four others for the model's attention. |
| 5 | **Baseline / ablation rows** — no-RAG, k=3, k=5, k=10 | **"80%" means nothing without a comparison row.** This is the difference between a number and an argument. Nearly free: it's a loop you're already running. |
| 6 | **Per-question-type breakdown** | *"Straightforward 90%, multi-hop 33%"* is a finding. A blended 78% hides it. A `groupby`. |

> **Not measured: precision@k.** With one correct chunk per question, perfect retrieval
> still scores 1/5 = 20%. The metric is structurally capped and says nothing. Worth being
> able to explain why it was left out.

**Generation**

| # | Check | What it asks |
|---|---|---|
| 7 | **Faithfulness + answer relevance** (RAGAS) | Does the answer stick to the retrieved chunks, and does it answer *the question asked*? Relevance is free once faithfulness is wired. |
| 8 | **Answer correctness** — LLM-as-judge, a model bigger than those being judged, 1–5 rubric | Right vs ground truth |
| 9 | **Citation accuracy** — does each cited chunk *actually support* the sentence attached to it? | **Nothing else on this list catches it.** A right answer with the wrong chunk attached passes both faithfulness and correctness, while shipping a lie to a user — against a product whose entire promise is "answers cite their sources." |
| 10 | **Judge validation** — hand-label 10 judged answers, report agreement | Check 8 is one model's opinion reported as fact. If the judge agrees with me 9/10, the metric means something. If it's 6/10, I've been reporting noise. |

**System**

| # | Check | What it asks |
|---|---|---|
| 11 | **Cost + latency per model** — TTFT, total, $ per query | Three extra columns on a loop already running, and the payoff for the whole multi-model design — which otherwise measures one of its three axes and throws the other two away. |

**Plus: automated cross-user isolation test.** Two Clerk accounts; assert user B's retrieval
returns zero of user A's chunks. ~15 lines. Since Day 1 this project has claimed RLS *is*
the security boundary — this is the difference between claiming it and proving it.

4. **Failure mode analysis** — which questions failed, why, what would fix them. This is
   what interviewers actually read. Day 11.5 is the follow-through.

### Done when

- [ ] `eval_results.md` exists: per-model rollups, per-question detail, per-type breakdown.
- [ ] Every number has a comparison row next to it.
- [ ] Extraction fidelity is written down *before* any retrieval number is trusted.
- [ ] I can say out loud: "Hit rate was X%, we missed on questions like Y because of Z, and I know it's Z and not W because of check N."

**Do not skip. Do not compress. If Day 10 slips, cut error handling polish before you cut this.**

---

## Day 11.5 — Improvements, measured

**Goal:** Fix what Day 11 found, and have the numbers to prove each fix worked.

> **Everything here must come after Day 11.** Built first, each of these is an
> unfalsifiable bullet point — *"I added re-ranking"* proves nothing. Built second, each is
> a before/after with a number on it. That difference is the entire value.

| Item | What | Why | Cost |
|---|---|---|---|
| **Re-ranking** | Retrieve k=20 by vector, send those 20 + the question to Cohere's rerank endpoint, keep the top 5 | The standard production fix for naive vector RAG, and the `COHERE_API_KEY` is already in `.env`. Verify the current rerank model ID before use. | 1 afternoon |
| **Hybrid search** | Postgres `tsvector` + `websearch_to_tsquery`, fused with vector results via Reciprocal Rank Fusion (~5 lines of SQL) | Dense retrieval degrades on **exact-match tokens** — identifiers, surnames, error codes. Postgres does full-text natively, so this is one migration and zero new dependencies. Only measurable because step 1 put a technical document in the corpus. | half day |
| **Prompt injection test + defense** | Upload a PDF containing an instruction aimed at the model. Show whether it obeys. Defend (delimiters, system prompt naming retrieved text as untrusted **data**), re-measure. | This app's input is *arbitrary files uploaded by strangers* — the textbook injection surface. Even "it partially works, here's the residual risk" is a more honest security answer than most candidates give. | 2h |
| **Abstention threshold** | Below a similarity cutoff, skip the LLM entirely and return "I don't see this in your documents" | A product improvement, not just a metric: fewer confident wrong answers, and it saves money by not calling the model at all. **Day 11's data sets the number** — that's why it isn't a guess made on Day 7. | 30 min |

### Done when

- [ ] Each item has a before/after row in `eval_results.md`.
- [ ] Re-ranking's delta is stated with its latency and cost, not just its accuracy.
- [ ] The injection test's outcome is written down honestly, including what still gets through.

---

## Day 12 — README + demo video + final deploy

**Goal:** Portfolio-ready. Live URLs work. Everything documented.

### Steps

1. **README.md** (top-level, replaces the auto-generated one):
   - Title + one-line pitch
   - Screenshot or GIF at the very top
   - **Live demo link** — no key needed, it's a rate-limited demo on my keys ("sign in, upload a doc, chat"). State the daily caps honestly.
   - **Architecture diagram** — Mermaid, showing: Browser → Vercel (Next.js) → Railway (FastAPI) → Supabase (Postgres + pgvector + Storage) + LangSmith side-branch + LiteLLM → Gemini/OpenAI
   - **Tech stack** with one-line "why" per choice
   - **Design decisions** — 3-4 paragraphs on:
     - Why pgvector instead of a dedicated vector DB
     - Why 800-token chunks with 100 overlap
     - Why LiteLLM as the abstraction point
     - How RLS enforces isolation (single security boundary)
   - **Public LangSmith trace link** (from Day 10)
   - **Eval results table + failure mode analysis** (Day 11), **with the Day 11.5
     before/after rows** — the fixes are worth more than the failures
   - **Local dev setup** — clone, env setup, migrations, running both apps
   - **What's next** — things deliberately *not* built, each with the one-line reason.
     Naming your own gaps reads as confidence; a list of unqualified "future work" doesn't.
     - **Position bias / chunk ordering** — the lost-in-the-middle effect is documented at
       long contexts. We send ~4K tokens into a 400K window, so it would measure as noise.
     - **Content-hash dedup** — uploading the same file twice mints a new `document_id` and
       a second full set of chunks (the unique index is on `(document_id, chunk_index)`, so
       it stops nothing across documents). Retrieval can then return the same passage five
       times and call it five sources. A feature, not an eval.
     - **CI regression eval on every PR** — real money per push, and no team to protect.
     - **Chunk-size ablation (400/800/1200)** — every variant re-embeds the whole corpus,
       paid per token.
     - **HyDE / query expansion** — re-ranking delivers more for less.
     - Ollama, semantic chunking, semantic caching.

     *(Hybrid retrieval and re-ranking moved **out** of this list — they're built and
     measured on Day 11.5.)*
2. **Demo video** — 2-3 minutes, narrated screen recording:
   - Sign up
   - Upload a doc
   - Wait for ingestion (skip forward if long)
   - Ask 3 questions
   - Switch model mid-conversation
   - Expand a citation
   - Show one LangSmith trace in the LangSmith UI
   - Upload to Loom or YouTube unlisted; link in README.
3. Final deploy of both apps. Smoke test on live URLs: sign up as brand-new user, upload, chat, see citations, log out, sign in as different user, verify isolation.

### Done when

- [ ] README reads like a senior engineer wrote it. Someone can understand what the app does and how it works in 3 minutes.
- [ ] Live demo URL works end-to-end for a stranger who's never seen the app.
- [ ] Demo video linked in README.
- [ ] Public LangSmith trace linked in README.
- [ ] Eval results in README.

---

## Reality check

- **This is ~15 days of focused work.** Grew from 12 on 2026-08-07: Day 11 became two days
  (3 metrics → 11) and Day 11.5 is new. The added days are all evaluation and measured
  improvement, which is the part of this project worth the most per hour spent on it.
  Full-time: ~3 weeks calendar. Evenings around job search: 4-5 weeks.
- **Something will break.** Most likely candidates: Clerk JWT → Supabase RLS handshake (Day 3), Railway Python cold-start (Day 3 evening), SSE streaming through Vercel/Railway (Day 7 or 8), Supabase Storage RLS policy syntax (Day 5).
- **Budget for slip.** If a day slips, extend calendar time — do not cut the eval.

## Cut order if time runs out

Cut in this order, most cuttable first:

1. Auto-titling conversations (Day 9)
2. Rename conversation (Day 9)
3. Delete conversation via UI (Day 9)
4. **Hybrid search (Day 11.5)** — first of the new work to go. Half a day, and if the
   technical document slips out of the corpus it has nothing to win on and returns a null
   result. Explaining precisely *when* BM25 beats dense retrieval, in the README, buys most
   of the same credit for free.
5. Error handling polish (Day 10 second half)
6. **Prompt injection test (Day 11.5)** — a strong story, but the only item here that
   improves nothing a user can see.
7. Multi-hop, adversarial and multi-turn questions (Day 11) — drop from 18 to 10
   straightforward Q&A pairs. **Last resort:** it guts checks 6 and the Day 9 measurement.

Do **not** cut:

- Eval script existing at all
- **Extraction fidelity (check 1)** — 2 hours, no code, and every number below it is
  untrustworthy without it
- **Baseline / ablation rows (check 5)** — free, and they are what turn a metric into an
  argument. An eval with no comparison row is a number floating in space.
- **Re-ranking (Day 11.5)** — the strongest measurable improvement available, and the key
  is already paid for
- LangSmith tracing
- README design-decisions section
- Demo video

---

## Definition of done for the whole project

- [ ] Live URL works
- [ ] Anyone can sign up, upload a doc, and chat — no key needed, caps stated honestly
- [ ] README explains why every major decision was made
- [ ] Eval results are public and show honest numbers (including failures)
- [ ] Public LangSmith trace exists
- [ ] Demo video linked
- [ ] I can walk through the whole thing in a 30-min interview and defend every choice
