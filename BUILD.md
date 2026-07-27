# BUILD.md

12-day build plan. Read `CLAUDE.md` first for stack + working agreement.

Each day has a **Goal**, **Steps**, and a **Done when** checklist. Don't skip the Done-when — if it doesn't pass, the day isn't finished.

---

## Project pitch (one line)

> Multi-model RAG chat over your documents. Upload PDFs, DOCX, or TXT. Pick your model. Bring your own key. Answers cite their sources.

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

### Steps

1. Deps: `pymupdf`, `python-docx`, `langchain-text-splitters`, `tiktoken`. (LiteLLM already installed.)
2. `apps/api/app/services/ingestion.py`:
   - `download_from_storage(file_path) -> bytes`
   - `parse(bytes, mime_type) -> str` — dispatch to PyMuPDF / python-docx / plain read
   - `chunk(text) -> list[Chunk]` — `RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=100, length_function=<tiktoken counter>)`
   - `embed(chunks) -> list[list[float]]` — `litellm.embedding(model="text-embedding-3-small", input=[...])`, batch requests
   - `ingest(document_id)` — orchestrates: update status → download → parse → chunk → embed → insert chunks → update status
3. Trigger from `POST /documents/upload` via FastAPI `BackgroundTasks`.
4. Status transitions: `pending` → `processing` → `ready` | `failed`. On failure, store the error message on the `documents` row (add an `error` column via a migration).
5. Frontend: poll `GET /documents` every 3s while any doc is `pending` or `processing`; show status badge.
6. Add `OPENAI_API_KEY` to `.env.example` and Railway env.

### Done when

- [ ] Upload a PDF → status flips through `pending` → `processing` → `ready` within ~30s for a small doc.
- [ ] `chunks` table has rows with 1536-dim vectors, correct `user_id` and `document_id`, sequential `chunk_index`.
- [ ] Uploading a broken file → status = `failed`, error message visible.

---

## Day 7 — RAG core

**Goal:** Chat endpoint that retrieves and generates streaming answers.

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
3. Supported models (values the frontend can send):
   - `gpt-4o-mini`
   - `claude-3-5-haiku-20241022`
   - `gemini/gemini-2.0-flash`
4. Add `ANTHROPIC_API_KEY`, `GEMINI_API_KEY` to `.env.example` + Railway.

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

### Done when

- [ ] Close browser → reopen → click an old conversation → see full history → keep chatting.
- [ ] Rename a conversation.
- [ ] Delete a conversation.
- [ ] First user message → conversation title auto-updates within a few seconds.

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

## Day 11 — Evaluation ⭐

**Goal:** Prove the RAG pipeline actually works. This is the interview-gold day.

### Steps

1. Test corpus: **3-4 Paul Graham essays** (public, unambiguous, well-known). Download the plain text, upload as documents in a fresh test account.
2. Write **15 Q&A pairs** by hand, in `scripts/eval_qa.json`:
   - 10 straightforward retrieval questions ("What does X say about Y?") with the ground-truth chunk noted.
   - 3 multi-hop questions requiring 2+ chunks to answer correctly.
   - 2 adversarial questions where the answer is *not* in the corpus — correct behavior is "I don't see this in the provided documents."
3. `scripts/eval.py`:
   - Loads Q&A pairs.
   - For each question, runs through the full pipeline (`/chat` endpoint or the underlying service directly).
   - For each of the 3 models: `gpt-4o-mini`, `claude-3-5-haiku`, `gemini-2.0-flash`.
   - Metrics:
     - **Retrieval hit rate**: did the ground-truth chunk appear in top-5?
     - **Faithfulness** (via RAGAS): does the answer stick to retrieved chunks?
     - **Answer correctness**: LLM-as-judge — use Claude 3.5 Sonnet or GPT-4o (something bigger than the models being judged), score 1-5 with a clear rubric, take mean.
   - Output: `eval_results.md` — markdown table with per-model rollups + per-question breakdown.
4. Write a **failure mode analysis** section: which questions failed, why, what would fix them (better chunking? re-ranking? query expansion?). This is what interviewers actually want to read.

### Done when

- [ ] `eval_results.md` exists with per-model metrics and per-question detail.
- [ ] I can explain out loud, in an interview: "Retrieval hit rate was X%, we missed on questions like Y because of Z, the fix would be W."

**Do not skip. Do not compress. If Day 10 slips, cut error handling polish before you cut this.**

---

## Day 12 — README + demo video + final deploy

**Goal:** Portfolio-ready. Live URLs work. Everything documented.

### Steps

1. **README.md** (top-level, replaces the auto-generated one):
   - Title + one-line pitch
   - Screenshot or GIF at the very top
   - **Live demo link** + BYOK instructions ("Get an OpenAI/Anthropic/Gemini API key here, paste in Settings, upload a doc, chat")
   - **Architecture diagram** — Mermaid, showing: Browser → Vercel (Next.js) → Railway (FastAPI) → Supabase (Postgres + pgvector + Storage) + LangSmith side-branch + LiteLLM → OpenAI/Anthropic/Gemini
   - **Tech stack** with one-line "why" per choice
   - **Design decisions** — 3-4 paragraphs on:
     - Why pgvector instead of a dedicated vector DB
     - Why 800-token chunks with 100 overlap
     - Why LiteLLM as the abstraction point
     - How RLS enforces isolation (single security boundary)
   - **Public LangSmith trace link** (from Day 10)
   - **Eval results table + failure mode analysis** (from Day 11)
   - **Local dev setup** — clone, env setup, migrations, running both apps
   - **What's next** — v1.5 ideas (Ollama, semantic chunking, hybrid retrieval, re-ranking)
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

- **This is 12 days of focused work.** Full-time: 2-2.5 weeks calendar. Evenings around job search: 3-4 weeks.
- **Something will break.** Most likely candidates: Clerk JWT → Supabase RLS handshake (Day 3), Railway Python cold-start (Day 3 evening), SSE streaming through Vercel/Railway (Day 7 or 8), Supabase Storage RLS policy syntax (Day 5).
- **Budget for slip.** If a day slips, extend calendar time — do not cut the eval.

## Cut order if time runs out

Cut in this order, most cuttable first:

1. Auto-titling conversations (Day 9)
2. Rename conversation (Day 9)
3. Delete conversation via UI (Day 9)
4. Error handling polish (Day 10 second half)
5. Multi-hop and adversarial questions (Day 11) — drop from 15 to 10 straightforward Q&A pairs

Do **not** cut:

- Eval script existing at all
- LangSmith tracing
- README design-decisions section
- Demo video

---

## Definition of done for the whole project

- [ ] Live URL works
- [ ] Anyone can sign up, paste a key, upload a doc, chat
- [ ] README explains why every major decision was made
- [ ] Eval results are public and show honest numbers (including failures)
- [ ] Public LangSmith trace exists
- [ ] Demo video linked
- [ ] I can walk through the whole thing in a 30-min interview and defend every choice
