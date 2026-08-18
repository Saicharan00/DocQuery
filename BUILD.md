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

- [x] `curl -N` on `/chat` streams tokens live. **Verified on Railway 2026-08-11**, which is
      the only place it means anything: `sources` landed at 1.72s and the first token at
      3.66s, a 1.94s gap a buffering proxy cannot produce. `X-Accel-Buffering: no` came back
      in the response headers.
- [x] Answer references content from an uploaded doc — cited `[1]` and `[3]` across
      ionospheric, tropospheric, multipath and DOP effects from a 77-page GPS report.
- [x] `sources` in the saved message contains `{document_name, chunk_index, content_preview}`
      for each retrieved chunk — all nine fields present, read back out of Postgres.
- [ ] Trying to chat over another user's docs is impossible (RLS blocks retrieval).
      **Still unproven** — needs a second Clerk account. `match_chunks` is `security invoker`
      so the policies from `001` should apply inside it, but "should" is not a test. Day 11's
      automated cross-user isolation test is where this gets settled.

**Also proven on the way through:** images retrieved *and read* (the model described a
diagram whose stored text is only `[Image from page 74]`); both providers answering through
the real endpoint; continuing an existing conversation; the spend cap returning `429` before
any billable call.

**Measured, for later days:** time-to-first-word was 3.66s, of which **1.72s is our own
pipeline** before the model is called — spend check, Cohere embedding, vector search, image
downloads, conversation insert, each a separate round trip. The half we control is as large
as the model's. Day 10's tracing is what breaks it down. Retrieval similarities came back at
0.18–0.25 against 0.68 for a near-verbatim quote on Day 6, with four of the top five chunks
being images — direct input for Day 11.5's abstention threshold.

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

- [x] Hold a real conversation on the live site. **Verified on Vercel 2026-08-11.**
- [x] Switch models mid-conversation — next message uses the new model.
- [x] Expand citations, see the chunks — numbering matches the `[n]` markers in the answer.
- [x] Kill the network mid-stream → get a clean error, not a hung UI. **Failed on first
      attempt and was fixed.** A vanished network never closes the socket, so `reader.read()`
      stayed pending forever and the page hung on "Thinking…". Each read now races a 20s
      idle timer that resets on every chunk. Verified live by switching wifi off mid-answer.

Route shipped as `/dashboard/chat`, not `/dashboard/chat/[id]` — an id in the URL buys
nothing until `GET /conversations/{id}/messages` exists. Day 9 moves the file into `[id]/`.

---

## Day 9 — Conversation history

**Goal:** Conversations persist. Users can resume old chats.

> **Split into 9a and 9b on 2026-08-12**, the same way Days 3 and 6 were.
> **9a — conversations persist** (steps 1–4): shipped and verified 2026-08-12.
> **9b — chat memory** (step 5): history in the prompt, and query rewriting before
> embedding. Shipped and verified 2026-08-12. Deferred to here not for scope but because
> Day 11 is what measures whether either actually helped, and neither is worth guessing at
> first.

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

- [x] Close browser → reopen → click an old conversation → see full history → keep chatting.
      **Verified 2026-08-12** by reload rather than a full browser restart — the same proof,
      since nothing client-side survives either. Citations came back with the answers, so
      the `sources` JSON round-trips through Postgres intact.
- [x] Rename a conversation. **Verified 2026-08-12.**
- [x] Delete a conversation. **Verified 2026-08-12**, including deleting the one currently
      open, which redirects to a new chat instead of leaving a dead page.
- [x] First user message → conversation title auto-updates within a few seconds.
      **Verified 2026-08-12** — "can you tell me what this document says about the things
      tha…" became "Positioning Accuracy Factors".
- [x] A follow-up ("what about the second one?") retrieves the right chunks, not noise.
      **Fixed by Day 9b, verified 2026-08-12.** It was broken earlier the same day: "give
      count" after a list of names was embedded literally, retrieved noise, and the model
      correctly refused to answer rather than inventing a number. That refusal was
      `SYSTEM_PROMPT` working; the retrieval was the defect. After 9b the server logs
      `Rewrote 'give count' as 'How many factors affect GPS positioning accuracy according
      to the document?'`, retrieval returns five chunks from consecutive pages 74–78 of the
      right document (top similarity 0.59), and the answer comes back grounded and cited
      with the correct count of 6.

### What was learned

- **`PATCH` was the hinge, and it passed.** The first write to an *existing* row anywhere in
  this app. PostgREST answers an RLS-rejected write with an empty result set rather than an
  error, so a broken rename would have returned `200 OK` and changed nothing — the status
  code proves nothing, and only reading the title back out proves the write landed. Both
  `PATCH` and `DELETE` therefore look the row up first and write second.
- **The `204` from `DELETE` is what proves the cascade.** `messages.conversation_id` is a
  foreign key, so Postgres refuses to delete a parent row that still has children. A delete
  that succeeds against a conversation with eight messages means those eight went with it.
- **`window.prompt()` is not universally available.** It threw `prompt() is not supported`
  in the browser, before any request could be sent, while `confirm()` in the same component
  worked. Rename is an inline text input instead — no dialog, nothing for a browser to
  block, and better UI regardless.
- **Deviation from step 1 above, chosen deliberately:** the sidebar lives in
  `apps/web/src/app/dashboard/chat/layout.tsx`, not the whole `/dashboard/*` layout, so the
  documents page stays a full-width upload screen.

#### From 9b

- **`order by created_at` on `messages` was a genuine coin flip, and had been since Day 1.**
  `created_at` defaults to `now()`, and Postgres' `now()` is the *transaction* timestamp — so
  the two rows `_save_exchange` writes in one insert carry byte-identical timestamps. Nothing
  guaranteed which came back first. 9a worked by luck. Both readers now add `role` as a
  second sort key, and the directions differ *because* the time directions differ:
  `list_messages` sorts time ascending so `role` is `desc` ('user' above 'assistant');
  `load_history` sorts time descending to grab the recent end, so `role` is ascending and the
  whole list is reversed afterwards. It looks like a typo and is commented in both files.
- **The model answers the ORIGINAL question, never the rewrite.** The rewrite exists to
  produce a better *vector*: it is embedded, logged, and discarded. It is never saved, never
  shown, and never sent to the answering model — history in the prompt is what makes the
  original question legible. Substituting it would answer a question the user never typed.
- **A heuristic gate on rewriting was proposed and dropped.** "Only rewrite short or
  pronoun-y questions" would have skipped `"give count"` — no pronoun, and short in a way no
  length rule catches on purpose. The one case 9b exists to fix would have been the one case
  the optimisation missed. The rewrite now fires whenever history is non-empty, which costs
  nothing on a first message because there is no history to find.
- **The hinge was verified standalone before anything depended on it.** `rewrite_query` is
  the only piece here that can return `200`-equivalent success and still not fix the bug,
  because "did this retrieve better chunks?" is a judgement about quality that no status code
  reports. It was called directly from a throwaway script with the real failing exchange
  hardcoded, and the strings were read by eye.
- **That reading caught a real prompt bug and one real limitation.** First pass, the rewriter
  stapled the conversation's topic onto questions that already stood alone — harmless inside
  one subject, wrong the moment a user pivots to a different document. Two added rules fixed
  it, confirmed by a pivot case that now passes through untouched. What did *not* get fixed:
  ordinals. `"explain the third one"` resolved to the second item in a list. Cheap models
  count list positions badly and no prompt rule reliably fixes it; retrieval is fuzzy enough
  to usually survive (neighbouring items sit in neighbouring chunks) and the answering model
  still sees the original wording. Marked with a `ponytail:` comment in `rag.py`. **Day 11
  measures whether that ceiling actually bites.**
- **Backend only — zero frontend files changed.** The browser already sent `conversation_id`,
  and history lives in Postgres, so the server reads it without being told anything new.

---

## Day 10 — LangSmith + error handling polish

**Goal:** Every query traced. Errors are user-friendly.

> **Split 10a / 10b on 2026-08-13**, the same way Days 3, 6 and 9 were.
> **10a — tracing** (steps 1–6). **10b — the error sweep** (steps 7–8), with the
> open bugs from the Day 10 pre-work folded in. 10a's code is complete; see the
> status block below the steps for exactly what is and is not verified.

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
   LANGSMITH_API_KEY=
   LANGSMITH_TRACING=true
   LANGSMITH_PROJECT=<project-name>
   LANGSMITH_ENDPOINT=https://api.smith.langchain.com
   ```
   > Renamed from `LANGCHAIN_*` on 2026-08-13. The old names were **not** broken —
   > `langsmith/utils.py` `get_env_var` takes `namespaces=("LANGSMITH", "LANGCHAIN")`
   > and tries both, so they are genuine aliases. `LANGSMITH_*` is simply the current
   > documented spelling. `LANGSMITH_ENDPOINT` is new and matters if the account is
   > in the EU: the default host is the US one, and an EU account with the US
   > endpoint sends traces nowhere.
7. Error handling sweep across the app:
   - LLM: invalid API key, rate limit, timeout, oversized context → user-facing messages
   - Upload: unsupported type, too large, corrupt file
   - Retrieval: no docs uploaded yet → "Upload a document first"
   - Auth: token expired → clean re-auth flow
8. No `console.error` or `print(e)` left as the only response to a failure.

### Done when

- [x] Every chat message produces a visible LangSmith trace with the full pipeline breakdown.
      *(Verified locally 2026-08-14: one trace, 7 spans, 0 detached. Railway still to confirm.)*
- [ ] One trace is public — URL saved.
- [ ] Every error path shows the user something meaningful. *(10b)*

### Day 10a — what it proved, and what it did not

**Two corrections to the 10a plan, both found by reading the SDK and then measured
rather than argued.** Neither would have raised an error — both produce a dashboard
that is quietly wrong.

1. Creating the root run sets **no context variable**, so the `@traceable`
   decorators cannot see it. The pre-flight needs an explicit "adopt this parent"
   block or its six spans each open a separate top-level trace.
2. Inside the streaming generator, that block must sit **directly around the
   streaming loop**, not at the top. Starlette pulls the generator one chunk at a
   time, each pull a fresh thread hop with a fresh copy of the context, so a block
   opened above the first `yield` is already gone when the loop starts. Measured
   through the real `iterate_in_threadpool`: at the top → `parent=None`; around the
   loop → attached.

**Verified:** one trace per question with the pipeline nested under it; the
`retrieve` span carrying similarity scores; the Day 9b rewrite visible and
*working* — `"give more detail"` became a standalone question and lifted
similarities from **0.21 to 0.60**; image payloads redacted to `<image: N KB>`
placeholders (16 of them, zero base64 uploaded); the app boots and answers
normally with tracing switched off.

**Not verified, and honest about it:**

- **Client disconnect leaves the trace open.** Starlette's `iterate_in_threadpool`
  has no `finally`, so an abandoned generator is never closed. One cause, three
  symptoms: no `_save_exchange`, no closed trace, and a model stream left open and
  billable. **Deferred to 10b**, where it lands with the `_save_exchange` decision
  it shares a fix with.
- **The zero-documents 400 path** — needs an account with nothing uploaded.
- **Railway**, and therefore the public trace URL.

**One real bug surfaced by tracing, cause still unknown.** On one request all four
image chunks failed to download and the answer went out looking perfectly normal,
built from text alone. Not reproducible since: the files exist, the paths are
right, RLS permits them, and both a later request and a fresh cold process loaded
them fine. `load_images` now logs the exception class and message instead of
swallowing them, so the next occurrence names itself.

**Added on 10a, not in the original plan.** Both asked for on 2026-08-14.

- **Answer feedback.** 👍/👎 under each answer, plus an optional comment, written
  onto that answer's LangSmith trace by `POST /feedback`. No new table and no
  migration: a rating is only worth having next to the retrieval and the prompt
  that caused it. The thumb and the comment are filed under **different keys**, so
  the score column stays one vote per answer and remains meaningful to average.
  **Migration 006** stores the run id on the answer's `messages` row, so an
  answer you come back to is still ratable — which matters, because the answer
  worth complaining about is usually the one you returned to. It also lets the
  endpoint *check* the run instead of trusting it: the rating is verified against
  a row `messages_isolation` will only show its owner. Answers written before 006
  have no run id and correctly show no buttons.
- **The Clerk user id is kept out of shareable traces.** It appeared in two
  places, and only one was obvious: the root metadata, *and* the first segment of
  every image path inside the `retrieve` span. Metadata now carries a short
  one-way hash, and storage paths have that segment swapped for `<user>`.
  **Traces created before this change still contain the raw id.** Note that a
  public trace still shows the document's *name* and the retrieved text, which is
  the more revealing part — pick the trace accordingly.

**Measured, for later days:** `embed_query` costs **7.70s on the first call of a
cold process and 0.11–0.42s afterwards**. Day 7 put the whole pre-model pipeline at
1.72s, so the first question of any deploy is an outlier, not the norm — worth
knowing before Day 11 reads timings off traces.

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

     > **This question now has a known suspect, found 2026-08-14. Expect it to fail.**
     >
     > Figures are embedded from **pixels alone**. `documents.py` stores an image chunk's
     > text as `[Image from page N]` and nothing else — no caption, no figure number — and
     > `ingestion.py` says so outright: *"The label is never what gets embedded, the picture
     > is."* The words that tell Figure 40 apart from Figure 17 live in a **separate text
     > chunk** with no link back to the picture.
     >
     > Technical block diagrams look alike — boxes, arrows, labels too small to read at the
     > render DPI — so their vectors cluster and the ranking between them is close to a coin
     > flip. Day 10a measured image similarities in a **0.18–0.25 band**, which is what that
     > looks like from the outside. Hand-testing on 2026-08-14 got roughly **1 usable figure
     > in 6 or 7** — an impression, not a measurement, which is exactly why it is written
     > here as a question to score rather than as a number to quote.
     >
     > **Ruled out:** the 75-image cap. That document has 69 image chunks, last image on page
     > 83, so nothing was lost to the cap. Do not re-run that theory.
     >
     > **Not a bug:** the embedding calls are correct — `input_type="image"` for pictures,
     > `search_document` for stored text, `search_query` for the question.
     >
     > Deliberately **not fixed before this measurement exists**, so Day 11.5 can carry a
     > real before/after row. The candidates are in the Day 11.5 list below.
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
| **Figure captions in the index** | Either widen each figure's crop downward to take in its caption line before rendering, so the picture itself carries the words "Figure 40 — Wi-Fi module"; or store a sibling **text** chunk per figure holding the caption plus nearby text and pointing at the same `image_path`. Hybrid search above only helps once one of these puts real words in `content`. | Figures are embedded from **pixels alone** — see the note on Day 11's figure-only question. Block diagrams look alike, so their vectors cluster and the choice between them is close to random. The crop is the smaller change; the sibling chunk is the more reliable one, but it needs `to_sources`/`build_messages` to dedupe so one figure is not cited twice. **Either way it needs a re-ingest, which re-spends embedding calls — a cost decision, not a free one.** | half day + re-ingest |
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
