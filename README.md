# DocQuery

## What this app is
This is a multimodal Retrieval-Augmented Generation (RAG) application.

**In plain English:**

You upload documents (PDFs, Word files, plain text) and then you can just ask
questions about them in a chat window, like texting a friend who has read every
page. The app finds the parts of your documents that actually answer your
question, hands them to an AI model, and the model writes an answer using only
that material, with footnotes pointing back to exactly which document and
which part it got the answer from. That includes charts, diagrams, and images
in your documents: the app finds the relevant picture, shows it to you right
alongside the answer, and the AI describes and summarizes what's in it, not
just the surrounding text. If it can't find the answer in your documents, it
says so instead of making something up. You can also pick which AI model
answers you, and switch models in the middle of a conversation.

It's a free live demo (you don't need your own API key, just sign in and try
it), but because I'm paying for every question out of my own pocket, there's
a daily limit per person so no one runs up the bill.

**In technical terms:**

This is a multimodal Retrieval-Augmented Generation (RAG) application. On
upload, documents are parsed (PyMuPDF for PDFs: text and page images,
python-docx for Word, plain read for text), then split into chunks using
LangChain's `RecursiveCharacterTextSplitter` (800 tokens per chunk, 100 token
overlap between chunks, counted with `tiktoken`). This is a deterministic
text-splitting algorithm, not a model. Each chunk (text or image) is embedded
with Cohere's `embed-v4` model into a shared 1536-dimension vector space, so
text and images are directly comparable to each other. Everything is stored in
a single Postgres database (via Supabase) using the `pgvector` extension, with
an HNSW index on the embedding column for fast approximate nearest-neighbor
search.

**Query time, step by step:**

1. If the conversation has history, the last 2-3 turns are sent to a cheap LLM
   call that rewrites the new question into a standalone one, e.g. "what about
   the second one?" becomes a question that names what "the second one" is.
   This matters because the rewritten question is what gets embedded; the raw
   follow-up alone would retrieve nothing useful.
2. The (rewritten) question is embedded and searched two ways at once: dense
   vector similarity via `pgvector`, and Postgres native full-text search
   (`tsvector` / `websearch_to_tsquery`) for exact-match terms like identifiers
   and proper nouns that embeddings alone tend to miss. The two result lists
   are merged with Reciprocal Rank Fusion (RRF).
3. The fused candidates are sent to Cohere's rerank endpoint, which re-scores
   them against the actual question and keeps the top 5. This catches cases
   where the initial vector/text search ranked a good chunk too low to make the
   cut.
4. If the best result's similarity is below a set threshold, the app abstains:
   it skips the LLM call entirely and returns "I don't see this in your
   documents." That's cheaper and more honest than a confident guess.
5. Otherwise, a prompt is built from the retrieved chunks (images are attached
   as actual image inputs, not just text) plus the last 2-3 turns of prior
   conversation for context. The question is not sent as a separate field;
   it's appended as the final piece of text inside that same message, after
   all the numbered sources, so the model reads the evidence first and the
   question last (models attend most reliably to the end of a prompt).
   Crucially, the question placed there is the **original**, as the user
   actually typed it, never the rewritten version from step 1. The rewrite
   exists only to produce a better search vector; the model answering the
   question needs the user's real wording, or it risks answering a question
   they didn't ask. This whole message is sent through LiteLLM, which routes
   the call to whichever model was picked (currently Gemini or OpenAI, both
   vision-capable so they can actually see the retrieved images).
6. The answer streams back token-by-token over Server-Sent Events, and once
   done, both the question and answer are saved along with the exact source
   chunks that produced it, so citations always point at real, re-checkable
   evidence.

Auth is Clerk; its JWT `sub` claim is passed straight through to Postgres,
where Row-Level Security (not application code) is what actually stops one
user from reading another user's documents, chunks, or conversations. The
whole pipeline is traced end-to-end in LangSmith, and its retrieval and answer
quality are measured by a custom eval suite (11 checks: extraction fidelity,
ANN recall, hit rate, MRR, faithfulness, citation accuracy, judge validation,
cost/latency, and more) rather than by feel.

**Stack:** Next.js (Vercel) → FastAPI (Railway) → Supabase (Postgres +
pgvector + Storage), Clerk auth, LiteLLM for chat routing, Cohere for
embeddings/rerank, LangSmith for tracing.

## Architecture

### 1. Whole-system flow

```
┌──────────┐   sign in    ┌───────────────┐
│  Browser │ ───────────▶ │  Clerk (Auth) │
│  (User)  │              └───────┬───────┘
└────┬─────┘                      │ JWT
     │ uses app                   ▼
     │                    ┌───────────────┐
     └──────────────────▶ │   Next.js     │
                          │   (Vercel)    │
                          └───────┬───────┘
                                  │ request + Bearer JWT
                                  ▼
                          ┌───────────────┐
                          │   FastAPI     │
                          │   (Railway)   │
                          └───┬───┬───┬───┘
              ┌───────────────┘   │   └───────────────┐
              ▼                   ▼                   ▼
     ┌────────────────┐  ┌────────────────┐  ┌────────────────┐
     │  Postgres +     │  │     Cohere     │  │    LiteLLM     │
     │  pgvector +     │  │ (embed/rerank) │  │ (chat routing) │
     │  Storage        │  └────────────────┘  └───┬────────┬───┘
     │  (Supabase)     │                          ▼        ▼
     └────────────────┘                    ┌────────┐  ┌────────┐
              ▲                            │ Gemini │  │ OpenAI │
              │ JWT forwarded,             └────────┘  └────────┘
              │ RLS enforces access
              │
     (same FastAPI box also
      writes traces to)
              │
              ▼
     ┌────────────────┐
     │   LangSmith     │   ← side branch, not in the live request path
     │  (tracing)      │
     └────────────────┘
```

**Reading it:** the browser only ever talks to Clerk (to sign in) and Next.js.
Next.js only ever talks to FastAPI, sending the Clerk JWT along. FastAPI is
the one component with credentials to everything else: it forwards that same
JWT to Postgres (so Row-Level Security enforces who can see what), calls
Cohere directly for embeddings/reranking, and calls LiteLLM for the actual
chat answer, which LiteLLM then routes to either Gemini or OpenAI depending on
what was picked. LangSmith sits off to the side, collecting a trace of each
step, but isn't something a request has to wait on.

### 2. Inside the FastAPI box

```
                        ┌─────────────────────────────────────────┐
                        │              FastAPI (Railway)            │
                        │                                            │
   incoming request  ─▶ │  ┌──────────────┐                          │
                        │  │   deps.py    │  verifies Clerk JWT       │
                        │  │ (auth +      │  (fetches JWKS, checks    │
                        │  │  DB client)  │  signature + expiry),     │
                        │  └──────┬───────┘  builds a per-request     │
                        │         │           Supabase client carrying│
                        │         │           that JWT                │
                        │         ▼                                  │
                        │  ┌───────────────────────────────────┐     │
                        │  │            routers/                │     │
                        │  │  health · me · documents · chat ·  │     │
                        │  │  conversations · feedback          │     │
                        │  └──────┬──────────────────────┬──────┘     │
                        │         │                       │            │
                        │         ▼                       ▼            │
                        │  ┌──────────────┐      ┌──────────────┐     │
                        │  │ services/     │      │ services/     │     │
                        │  │ ingestion.py  │      │ rag.py        │     │
                        │  │ (parse, chunk,│      │ (embed, hybrid│     │
                        │  │  embed docs)  │      │  search, rerank│     │
                        │  │               │      │  rewrite,     │     │
                        │  │               │      │  build prompt)│     │
                        │  └──────────────┘      └───────┬───────┘     │
                        │                                 │             │
                        │                                 ▼             │
                        │                        ┌──────────────┐      │
                        │                        │ services/     │      │
                        │                        │ tracing.py    │      │
                        │                        │ (LangSmith    │      │
                        │                        │  spans)       │      │
                        │                        └──────────────┘      │
                        │                                                │
                        │  config.py: env vars / settings, read once    │
                        │  at startup, injected into all of the above   │
                        └─────────────────────────────────────────┘
```

**Reading it:** every request hits `deps.py` first. That's the gate: no
valid Clerk JWT, no further access, and it's also what hands every other
piece of code a Supabase client already carrying that user's identity, which
is what makes RLS work everywhere downstream. `routers/` then just dispatches
by URL path to the right handler. The actual work happens in `services/`:
`ingestion.py` for the upload→chunks→embeddings pipeline, `rag.py` for the
whole query-time pipeline from the diagram above. `tracing.py` wraps calls in
both of those to send spans to LangSmith. `config.py` isn't in the request
path at all. It just loads env vars once at boot and gets used everywhere
else.

## Tech stack

**Frontend**

| Component | Why |
|---|---|
| Next.js 15 (App Router) | Handles routing + rendering in one framework, and deploys to Vercel with zero config. |
| TypeScript (strict) | Catches type mistakes before they become runtime bugs, worth it on a solo project with no code review. |
| Tailwind CSS | Style directly in markup, no separate CSS files to keep in sync. |
| shadcn/ui | Component code gets copied into the repo, not hidden in a package, so you can actually read and edit what you're using. |
| Vercel | Free tier, and it's built by the same people as Next.js, so deploys just work. |

**Auth**

| Component | Why |
|---|---|
| Clerk | Hosted sign-up/sign-in so none of that gets built by hand, and its JWT's `sub` claim maps directly onto Postgres RLS with no translation layer. |

**Backend**

| Component | Why |
|---|---|
| FastAPI | Async Python with native streaming support (needed for SSE), plus automatic request validation via Pydantic. |
| Railway | Simple deploy target for a Python service, env vars managed in one place. |
| uv | Fast, modern Python dependency manager. |

**Database + storage**

| Component | Why |
|---|---|
| Supabase (Postgres) | Managed Postgres with Row-Level Security built in: the actual security boundary for this app. |
| pgvector | Vector search lives in the *same* database as everything else, so there's no separate vector DB to keep in sync. |
| Supabase Storage | File storage in the same project, with path-prefix policies that reuse the same RLS model. |

**LLM + embeddings**

| Component | Why |
|---|---|
| LiteLLM | One interface for multiple chat providers, so the model is swappable per request without touching the calling code. |
| Cohere `embed-v4` | The rare embedding model that puts text *and* images in the same vector space, required for the image-retrieval feature. Called directly via Cohere's SDK, not through LiteLLM, since LiteLLM's abstraction is for chat, not embeddings. |
| Cohere Rerank | A cheap, dedicated reranking endpoint on a key already being paid for, used to fix retrieval ranking after hybrid search. |
| Gemini + OpenAI | Two providers so the LiteLLM abstraction is actually exercised, not just claimed; both vision-capable, which is a hard requirement since image chunks are sent as real image inputs. |

**Parsing + chunking**

| Component | Why |
|---|---|
| PyMuPDF | Pulls both text *and* page-region images out of PDFs, needed for the multimodal pipeline. |
| python-docx | The standard way to read `.docx` structure in Python. |
| `RecursiveCharacterTextSplitter` | Splits on natural boundaries (paragraphs, sentences) before falling back to a hard cut, instead of chopping mid-sentence. |
| `tiktoken` | Counts tokens the way the model actually sees them, so an "800-token chunk" is a real token budget, not just a character count. |

**Retrieval extras**

| Component | Why |
|---|---|
| Postgres full-text search (`tsvector`) | Catches exact-match terms (IDs, names) that embeddings tend to blur past; native to Postgres, zero new dependencies. |
| Reciprocal Rank Fusion | Combines two rankings that live on incomparable scales (cosine similarity vs. `ts_rank`) without one dominating just because its numbers are bigger. |

**Observability + eval**

| Component | Why |
|---|---|
| LangSmith | Traces every step of the pipeline: what made the pre-model latency, a context-adoption bug, and an abandoned-stream bug all findable instead of guessed at. |
| RAGAS | Standard library for faithfulness/relevance scoring, instead of writing that judgment logic from scratch. |

## Design decisions

**Why pgvector instead of a dedicated vector database.** A dedicated vector
store (Pinecone, Weaviate, Qdrant) would put the embeddings in a second
system that has no idea what Postgres's Row-Level Security policies say. This
project's whole security model rests on one rule: RLS, not application code,
decides which rows a user can see. Splitting the data across two databases
would mean re-implementing that access check by hand in a system that has no
concept of Clerk's JWT, which is exactly the "trust the backend's WHERE
clause" pattern this project explicitly refuses to rely on. `pgvector` keeps
the vectors as ordinary columns in the same tables the rest of the app
already trusts, so one set of policies protects both the text and the
embeddings, with nothing extra to keep in sync.

**Why 800-token chunks with 100-token overlap.** A chunk has to be large
enough to stand on its own (a 50-token fragment rarely contains a complete
thought worth retrieving) but small enough that one chunk stays about one
idea, since a chunk that spans several ideas dilutes the similarity score for
all of them. 800 tokens is a common middle ground for prose documents. The
100-token overlap exists for the boundary case: if a sentence that answers a
question gets cut in half by a chunk boundary, the next chunk carries the
last 100 tokens of the previous one, so the sentence still shows up whole in
at least one chunk instead of vanishing between two incomplete halves. This
number was set once and never re-tuned. A chunk-size ablation (comparing
400/800/1200) would answer whether it's actually optimal, but every variant
means re-embedding the entire corpus at real API cost, so it's listed under
"what's next" rather than guessed at for free.

**Why LiteLLM, and why only for chat.** The app answers through two different
providers (Gemini and OpenAI), and LiteLLM is what makes that a config value
instead of two separate SDKs wired into the same code path. Adding a model
means adding one line to an allowlist, not rewriting the request builder.
Embeddings deliberately stay outside LiteLLM and call Cohere's SDK directly.
The reason is that Cohere's `embed-v4` does something no generic embeddings
abstraction is built around: it puts text and images in the same vector
space, which is what lets an uploaded chart get retrieved and read the same
way a paragraph does. LiteLLM's abstraction is for swappable chat models;
forcing the one embeddings provider this app actually needs through that
same layer would add a dependency without adding a real choice.

**How RLS enforces isolation.** Clerk issues a JWT with the user's ID in its
`sub` claim. That JWT is not just used to authenticate the request into
FastAPI: it is forwarded, unmodified, to Postgres on every single database
call, where PostgREST sets it as `request.jwt.claims`. Every table that holds
user data has a policy that reads that claim and filters rows against it, at
the database level, before any application code runs. This matters because
it means a bug in the backend, a forgotten `WHERE user_id = ...`, an endpoint
that queries too broadly, cannot leak another user's data, since the database
itself refuses to return rows the policy doesn't allow. This was a claim from
Day 1 of this project and stayed a claim until Day 11, when an automated
test using two real Clerk accounts confirmed it directly: user B's retrieval
returns zero of user A's chunks.

**Why hybrid search uses Postgres full-text search, not BM25.** The
"keyword" half of hybrid search is Postgres's built-in `ts_rank` scoring a
`tsvector` column, not true BM25. The two are similar in spirit (both reward
exact term matches that embeddings can miss) but use different math, and
real BM25 in Postgres needs an extension like ParadeDB's `pg_search` that
isn't part of this stack. `tsvector` was chosen because it ships with
Postgres already: zero new dependencies, one migration.

## Eval results

Full breakdown, every question, and the raw judge output live in
[`scripts/eval_results.md`](scripts/eval_results.md). This section is the
summary: what the numbers say, what actually failed, and what fixed it.

Corpus: 3-4 Paul Graham essays (plain prose) plus one 91-page technical
report with tables, figures, and identifiers. 18 hand-written questions
(10 straightforward, 3 multi-hop, 2 adversarial with no correct answer in
the corpus, 2 multi-turn, 1 answerable only from a figure), run against both
supported models.

### Upstream checks (run before trusting anything downstream)

| Check | Result |
|---|---|
| Extraction fidelity | 2 of 4 test documents degraded, 1 unusable, 1 clean by design. The 2-column academic PDF loses a table's column alignment; the table-heavy report scrambles one table's reading order; `python-docx` drops all 5 tables in the `.docx` fixture entirely (a known, commented limitation); the scanned PDF correctly falls back to image-only ingestion instead of failing. |
| ANN recall vs. exact search | 10/10 average overlap across all 18 questions. The approximate HNSW index is not losing anything on a corpus this size. |

### Retrieval

| k | Hit rate | Recall | MRR |
|---|---|---|---|
| 3 | 94% | 91% | 0.74 |
| 5 (production default) | 94% | 94% | 0.74 |
| 10 | 100% | 100% | 0.75 |

Per question type at k=5:

| Type | n | Hit rate | Recall | MRR |
|---|---|---|---|---|
| Straightforward | 10 | 90% | 90% | 0.73 |
| Multi-hop | 3 | 100% | 100% | 0.67 |
| Multi-turn | 2 | 100% | 100% | 1.00 |
| Figure-only | 1 | 100% | 100% | 0.50 |

### Generation

| Model | Correctness, RAG | Correctness, no-RAG | Faithfulness | Answer relevancy |
|---|---|---|---|---|
| gemini-3.5-flash-lite | 4.56/5 | 2.00/5 | 0.84 | 0.65 |
| gpt-5.4-nano | 4.56/5 | 1.28/5 | 0.85 | 0.76 |

Retrieval more than doubles answer correctness over no-RAG on both models,
which is the whole argument for building this in the first place.

**Citation accuracy: 76/81 cited sentences supported (94%).** This is the
check nothing else on the list catches: a right answer with the wrong source
attached would still pass correctness and faithfulness while shipping a lie
to a user, against a product whose entire promise is that answers cite their
sources. Most of the shortfall traced back to a judge limitation, not a real
problem: sentences that legitimately synthesize two cited chunks together
get marked unsupported by a rubric that checks each citation in isolation.

**Judge validation: 9/10 exact agreement with a human, 10/10 within one
point.** The correctness score above is one model's opinion; this is what
makes trusting it reasonable instead of reporting noise.

### Cost and latency

| Model | Avg time to first token | Avg total latency | Avg cost/question |
|---|---|---|---|
| gemini-3.5-flash-lite | 1.73s | 1.97s | $0.0006 |
| gpt-5.4-nano | 0.68s | 1.14s | $0.0004 |

Total spend across all 72 generation calls in this eval run: **$0.0353.**

### Cross-user isolation

**PASS.** Two real, separately signed-in Clerk accounts, same query vector.
User A's retrieval returned their 10 chunks; user B's retrieval on the
identical vector returned their own 10 (non-empty, so this isn't just "B had
nothing to find"), and zero overlap between the two sets. This is what turns
"RLS is the security boundary" from a claim held since Day 1 into a checked
fact.

### What actually failed, and why

Retrieval itself was not the bottleneck. There was exactly one real
retrieval miss and two real generation failures:

- **One retrieval miss (`sf-10`).** The ground-truth chunk ranked #7 at
  k=5 because the question's phrasing embedded closer to a neighboring
  chunk than to the "official" answer chunk, an artifact of chunking
  spreading one fact across adjacent chunks. The answer was still correct
  (the neighboring chunk that got retrieved instead happened to contain the
  same fact), so this cost nothing in practice, but it's the honest source
  of the 94% headline number instead of 100%.
- **Two generation failures**, neither an upstream bug. On a multi-turn
  question, the correct chunk was retrieved at rank 1, but one model
  literally read a passage's implicit answer as absent and refused to
  answer, while the other re-stated an earlier turn's answer instead of the
  new question. On the figure-only question, one model misread the order of
  two blocks in a retrieved diagram; the other read it correctly. Both are
  model reading-comprehension gaps, not retrieval or prompt-construction
  bugs.

### Day 11.5: measured, not just claimed

Built after Day 11, in the order Day 11's own findings pointed to, each with
a before/after number.

**Reranking fixed the one retrieval miss.** `sf-10`'s chunk moved from
rank #7 to rank #3 once Cohere's rerank endpoint re-scored the candidates
against the literal question.

| Stage | Hit rate (k=5) | Recall (k=5) | MRR (k=5) |
|---|---|---|---|
| Day 11 baseline (vector only) | 94% | 94% | 0.74 |
| + Hybrid search alone | 94% | 94% | 0.74 |
| + Reranking (final pipeline) | 100% | 100% | 0.854 |


**Abstention: a similarity gate, then a model judgment call.** The
original plan assumed a clean similarity gap between answerable and
unanswerable questions. Day 11's actual numbers didn't show one: the two
adversarial questions' similarity scores landed inside the range of
genuinely answerable questions, not below it. A conservative floor was set
below the lowest score seen on any real answerable question instead — it
caught clearly off-topic questions for free (no LLM call) but, by
construction, never caught the two adversarial ones.

It also turned out to refuse a class of question nobody had tried yet:
"what is this document about" never resembles any single passage closely,
so a perfectly good upload still scored below the floor. No fixed list of
"broad question" phrasings closes that gap for every rephrasing — one was
tried, and missed "what is this **book** about" on the very next test.
The similarity gate is retired. `SYSTEM_PROMPT` now instructs the model to
answer with the exact abstain message itself when the sources don't cover
the question, a call made by reading the actual content instead of scoring
word overlap with one chunk. The cost tradeoff flips: every question now
costs one paid call, including ones that end up abstaining — bounded by
the same per-user daily caps as everything else, and worth it for a
question shape a real visitor asks first.

**Prompt injection: a structural defense is now in place.** A realistic
injected instruction (a fake "your account is suspended" redirect hidden
inside an uploaded document) was tested against both models. After adding
the defense, both correctly ignored the injected instruction, answered the
real question, and kept their citation intact: `SYSTEM_PROMPT` now states
plainly that every retrieved source is untrusted document content, never a
command, and `build_messages` wraps each source in `<<<SOURCE>>>...
<<<END SOURCE>>>` delimiters so that boundary is structural, not just a
sentence the model could be argued past.

### Day 12: image captioning, measured

**Fixed the figure-retrieval ceiling.** Figures used to be embedded straight
from their pixels, which measured a ~0.18-0.25 similarity band against a
typed question — functionally noise. Each figure is now captioned by a
vision model at ingestion time, and the *caption* is embedded instead, in
the same vector space every text chunk already uses. The one figure question
in the eval set moved from rank #2 to rank **#1** (MRR 0.50 → 1.00,
similarity 0.4957); a direct caption-vs-question smoke test measured 0.5371,
well clear of the old band. All pre-existing image chunks were backfilled,
not just new uploads. Generation accuracy is unchanged by design — the
answering model still reads the real image, not the caption, so this fixes
*finding* the right figure, not *reading* it once found. Full numbers:
[`scripts/eval_results.md` §11](scripts/eval_results.md).

## Known limitations

Real gaps the eval surfaced, still standing:

- **`.docx` tables are silently dropped.** `python-docx` parsing only reads
  paragraph text; every table in a Word document, including all its cell
  values, never reaches the index. A doc that's mostly tables loses almost
  everything.
- **Complex PDF tables can lose their structure.** A dense, table-heavy PDF
  extracts with reading order that doesn't reliably track a table's visual
  position on the page, so a row's label and its value can end up separated
  in the parsed text.
- **A model can still misread a figure once it's found.** Day 12 fixed
  *finding* the right figure (see above) — it can't fix a vision model
  misreading what's in it. One of the two supported models has been
  observed misreading a diagram's block order even when it's looking at
  the correct image.
- **Abstention is a floor, not a classifier.** It reliably catches questions
  with nothing relevant in the corpus, but adversarial questions that sit
  inside the normal similarity range for legitimate questions get past it
  and reach the model instead of being caught upfront.

## What's next

Things deliberately not built, each with the reason it isn't. Hybrid search
and reranking used to be on this list, they're built and measured under
[Day 11.5](#day-115-measured-not-just-claimed) instead.

- **Position bias / chunk ordering.** The "lost in the middle" effect, where
  a model pays less attention to content in the middle of a long prompt, is
  documented at long context lengths. This app sends roughly 4K tokens of
  retrieved context into a 400K-token context window, small enough that the
  effect would measure as noise rather than signal.
- **Content-hash dedup.** Uploading the same file twice currently mints a
  new document and a second full set of chunks, since nothing checks whether
  an identical file already exists for that user. Retrieval can then return
  the same passage several times and present it as several independent
  sources. Worth fixing, but it's a straightforward one, not something that
  needs an eval to justify.
- **CI regression eval on every PR.** Real money spent per push, on a
  project with no team and no CI budget to protect.
- **Chunk-size ablation (400 / 800 / 1200 tokens).** Every size variant
  means re-embedding the entire corpus at real per-token cost, so 800/100
  was set once from common practice rather than tuned against this data.
- **HyDE / query expansion.** Reranking already delivers a bigger accuracy
  gain for less engineering and less added latency, so this wasn't worth
  building on top of it.
- **Semantic caching, scoped per conversation.** Embed the incoming
  question, and on a close match to an earlier question in the *same*
  conversation, replay that answer instead of paying for retrieval and
  generation again. Deferred, not dropped: a cache shared *across* users is
  the trap, since an answer is only valid for one user's own document set,
  and a cross-user hit would serve someone else's answer. Scoping the cache
  to one conversation keeps the document set fixed, which is what makes a
  hit safe, but with only a handful of demo visitors the hit rate likely
  wouldn't pay for the extra embedding call yet.
- **Ollama / local models.** This app is a hosted, API-key-funded demo, not
  something a visitor runs on their own hardware, so a local-inference path
  doesn't serve this app's actual users.
- **Semantic chunking.** A smarter alternative to fixed-size chunking, but
  the eval found no retrieval failures traceable to chunk boundaries, so
  there's no measured problem for it to solve yet.
- **Document-metadata questions** ("how many pages is this?", "when was
  this uploaded?"). The app only ever shows the model the handful of
  passages retrieval judged most relevant to the question, never the whole
  document, so a fact like total page count — true of the file, but not
  stated in any single passage's text — correctly reads as "not in the
  sources" rather than getting guessed. Answering this class of question
  would mean a separate, non-retrieval code path that reads metadata
  directly instead of searching content; not built, since retrieval-based
  Q&A was the actual thing being demonstrated.

