# Day 6a — Text Ingestion, Driven by the Browser

Personal learning log. Not read by Claude automatically — this is for me, to
recall what I built and why once the project is done. Not needed as context
for future sessions or development; it's a record, not a spec.

Day 6 was split in two. **6a** is text: an uploaded PDF/DOCX/TXT becomes
embedded, searchable chunks. **6b** is images, and is not built yet.

---

## Part 0 — Fixing the record before writing code

### 1. Three project documents described a product I wasn't building

Before any code, `CLAUDE.md` and `BUILD.md` were corrected. All three were
decisions already taken in earlier sessions that the docs had never caught up
with:

| Said | Reality |
|---|---|
| "No hosted API keys — user pastes their own" | BYOK was dropped. I fund every call, so the app is a rate-limited demo |
| Embeddings via OpenAI `text-embedding-3-small` | Cohere `embed-v4.0` |
| Day 7 chat models: `claude-3-5-haiku`, `gemini-2.0-flash`, `gpt-4o-mini` | Two of the three are retired |

This matters more than it looks. A spec that contradicts the plan doesn't just
sit there being wrong — it actively misleads whoever reads it next, including
me in three weeks and including Claude at the start of every session. Claude
re-derived a decision *incorrectly* this session precisely because its notes
hadn't recorded it.

### 2. Embeddings use the Cohere SDK. LiteLLM is for chat only

LiteLLM is the multi-provider abstraction for **chat**. It also technically
supports Cohere embeddings, and using it would have kept the backend to one
client library. I looked at its actual source before deciding
(`litellm/llms/cohere/embed/transformation.py:96-111`):

```python
is_encoded = is_base64_encoded(input_str)
if is_encoded:                      # guesses "image" by sniffing the string
    ...images=input, input_type="image"
else:
    ...texts=input, input_type=COHERE_DEFAULT_EMBEDDING_INPUT_TYPE
```

Two problems. A call is *all* text or *all* images — it infers which by
checking whether the string looks base64-encoded. And `input_type` isn't a
real parameter, it's a constant. Embed v4's whole value for Day 6b is
interleaving an image *and* its caption into one vector, which this shape
cannot express.

So: `uv add cohere`, and embeddings call the SDK directly. The trade-off
accepted is two client libraries in the backend. It costs little, because
LiteLLM's value is swapping chat providers — and there is no second embedding
provider to swap to anyway, since switching would invalidate every vector
already stored.

**Lesson:** an abstraction layer is only worth it where you'll actually
exercise the abstraction. Read the library's source before assuming it covers
your case.

---

## Part 1 — The credential problem, and the shape it forced

### 3. The work outlives the thing that authorises it

This is the whole reason Day 6a looks unusual.

- Embedding a document takes **minutes**.
- A Clerk session token lives about **60 seconds**.
- Every database write in this project rides on that token, because that's
  what RLS checks — which is why `documents.py` contains no
  `WHERE user_id = ...` anywhere.

BUILD.md originally said to run ingestion in a FastAPI `BackgroundTasks` job
after the response is sent. That job would outlive its own authorisation:
partway through, the token expires, Supabase rejects the write, and the
document dies mid-ingest.

Two obvious escapes, both rejected:

1. **Carry the user's token anyway.** Nothing new to configure, but it simply
   fails once the token expires, and a backend cannot refresh a Clerk token
   without the browser.
2. **Use `SUPABASE_SERVICE_ROLE_KEY` in the ingestion file.** That key bypasses
   RLS entirely. It works, but RLS stops being the guard on that path, the key
   owns the whole database if it leaks, and CLAUDE.md's "RLS is the security
   boundary" rule gains a permanent exception.

### 4. The answer: the browser drives the work in steps

`POST /documents/{id}/ingest/step` works for 45 seconds, writes what it
finished, and returns where it got to. The browser calls again with a fresh
token. Repeat until `done`.

```
browser  --step-->  server works ~45s, writes chunks 0..95
         <--{done:false, 96, 278}--
browser  --step-->  (fresh token) resumes at 96
         <--{done:false, 192, 278}--
                    ... until done:true, status = ready
```

Why 45 and not 60: the token has to still be valid when the *final* write
lands, so the budget leaves room for the round trip.

What this buys:

- **No service-role key anywhere.** RLS stays the only boundary.
- **No token ever has to outlive its lifetime.** Each step sits well inside one.
- **Resumability for free.** Closing the tab leaves the document at
  `processing` with its finished chunks saved; the next visit continues from
  there. That wasn't a feature I set out to build — it falls out of the shape.

The frontend needed no token machinery for this: `useApi` already calls
`getToken()` on every request (`apps/web/src/lib/api.ts:39`), so every step is
freshly authenticated already. The change was a loop, nothing more.

### 5. Resume works because chunking is deterministic

The resume point is `max(chunk_index)` already in the database. That number
only means anything if the same file always produces the same chunk list in
the same order. If chunking were random in any way, "resume at chunk 40" would
point at *different text* on the second call, and the document would end up
with overlapping and missing content — silently, with no error anywhere.

So determinism is a load-bearing property, and it gets a test:

```python
first = chunk(sample)
second = chunk(sample)
assert first == second, "chunk() is not deterministic — resume would corrupt data"
```

This is the only test in the project so far, and it exists because this is the
only place where being wrong produces no symptom.

### 6. The real guard is a database constraint, not Python

```sql
create unique index chunks_document_chunk_idx on chunks (document_id, chunk_index);
```

A duplicated or replayed step now fails loudly at the database instead of
quietly writing chunk 40 twice. Enforcing it in Python would mean enforcing it
in every code path that ever inserts a chunk; enforcing it here means it holds
no matter which path does the insert, including one I write in a month having
forgotten this conversation.

**Lesson:** when a rule must always be true, put it where it cannot be
bypassed — that is usually the schema, not the application.

### 7. The endpoint is `def`, not `async def` — deliberately

```python
@router.post("/{document_id}/ingest/step")
def ingest_step(...):        # not async
```

Every other handler in this project is `async def`. This one can't be. It
performs up to 45 seconds of *blocking* network calls (embedding). Inside an
`async def`, that blocks the event loop — meaning every other user's request,
including `/health`, waits 45 seconds behind one person's document.

FastAPI runs a plain `def` handler in a worker thread, so the loop stays free.
One keyword, and it's the difference between a service and a service that
freezes for anyone else while somebody uploads a PDF.

---

## Part 2 — Schema

### 8. Migration 003, with columns nothing uses yet

```sql
alter table documents add column error text;
alter table chunks add column chunk_type text not null default 'text' check (...);
alter table chunks add column image_path text;
alter table chunks add column page_number int;
create unique index chunks_document_chunk_idx on chunks (document_id, chunk_index);
```

The three image columns are 6b's, and nothing writes them today. They went in
anyway because **`chunks` is empty right now**. Adding columns to an empty
table is instant; adding them after 6a has filled it means re-ingesting every
document and paying for every embedding a second time.

That's not "building for later" in the speculative sense — it's noticing that
the cost of this specific change rises steeply with time, and that the design
calling for it already exists.

### 9. Migration 004 — reading past RLS without a master key

The global daily kill switch needs to count *everyone's* uploads today. But
RLS scopes every query to the caller, so a normal count returns only my own
rows. The version originally planned — "count what we can see" — would have
been the per-user cap wearing a different hat: it would look like a guard and
stop nothing.

The fix, without giving the app a service-role key:

```sql
create function public.documents_created_today()
returns integer
language sql
security definer                        -- runs as its creator, not its caller
set search_path = public, pg_temp       -- not optional; see below
stable
as $$ select count(*)::int from public.documents
      where created_at >= date_trunc('day', now() at time zone 'utc'); $$;

revoke all on function public.documents_created_today() from public;
grant execute on function public.documents_created_today() to authenticated;
```

`security definer` means the function runs with **its creator's** privileges
instead of the caller's, so it can see past RLS. That is a genuine privilege
escalation, so it's made as small as possible: no arguments, no table access
handed to the caller, one integer out. A caller learns *how busy the service
is* and nothing about whose documents those are.

`set search_path` is mandatory on such a function. Without it, someone could
create their own `documents` table in a schema earlier on the search path and
have my privileged function read *that* instead.

**Lesson:** "I need to bypass a security rule" often has a narrow answer — a
tiny, audited hole — rather than the broad one (hand the app a master key).

---

## Part 3 — The pipeline

### 10. Parse returns `(page_number, text)` pairs, not a blob

Page numbers flow into `chunks.page_number` so Day 8 can cite "page 41". DOCX
and TXT report page 1 rather than inventing numbers they don't have.

A file that yields no text raises with a message written for a human:

> No readable text found in this file. If it is a scanned PDF, the pages are
> images and there is no text to extract.

That case is common, not exotic — a scanned PDF is a stack of photographs. It
lands on `status='failed'` with that sentence stored on the row and shown in
the UI. (6b is what will eventually make those documents useful.)

### 11. Chunking: 800 tokens, 100 overlap, counted with tiktoken

A *token* is roughly a word-fragment; ~4 characters of English. Without a
tokeniser, "800" would silently mean 800 *characters* — about a quarter of the
intended size.

`RecursiveCharacterTextSplitter` splits on paragraph breaks first, then
sentences, then words — trying the largest separator that still fits. So a
chunk usually ends at a natural boundary rather than mid-sentence.

The 100-token overlap means a sentence sitting on a chunk boundary appears in
*both* chunks, so it can still be found.

Honest imprecision: tiktoken is OpenAI's tokeniser, and Cohere's differs by a
few percent. It's fine here because the count decides *where to cut text*, not
what gets billed.

### 12. `input_type` is the parameter that fails silently

```python
cohere.ClientV2().embed(
    model="embed-v4.0",
    texts=batch,
    input_type="search_document",   # ingestion
    output_dimension=1536,
    embedding_types=["float"],
)
```

Cohere embeds a *stored passage* and a *search query* differently, and
`input_type` says which. Ingestion uses `search_document`; **Day 7's question
must use `search_query`**. Get it wrong and there is no error, no warning —
just worse answers forever. Written into both the code comment and the plan
because it is invisible when broken.

`output_dimension=1536` is pinned rather than left to default, because the
column is `vector(1536)` with an HNSW index built for that width; anything else
is rejected by Postgres.

Two things verified against the installed SDK instead of assumed:

- `input_type` is a **required** argument of `ClientV2.embed`.
- The response field is `response.embeddings.float_` — with a trailing
  underscore, because `float` is a Python builtin.

That second one would have been a runtime crash on the first real call.

### 13. Batching

96 texts per request. Above that Cohere refuses. Kept as a named constant so a
rejected request points at one line.

---

## Part 4 — Money, and the limits it buys

### 14. Two caps, one of them honest about its hole

Since BYOK is gone, every embedding is billed to my key. So:

- **15 documents per user per 24h** → HTTP 429. Checked *before* the file is
  read, so a rejected upload doesn't cost 10MB of memory first. No `user_id`
  filter — RLS scopes the count, same as everything else in that file.
- **A global daily ceiling** → HTTP 503, via the function in §9.

The per-user cap has a known hole: deleting a document frees an allowance, so
a determined user can exceed it. It's marked in the code rather than papered
over:

```python
# ponytail: deleting a document frees an allowance, so a determined user can
# exceed the cap. Accepted — the cap is a spend brake, not a security control.
```

Naming what a guard *doesn't* do is worth more than pretending it's airtight.
Neither cap is a security control; both are spend brakes. Together they turn a
bad night from a large bill into a few hours of downtime.

### 15. A rate limit is not a failure — my first version got this wrong

The trial Cohere key allows **100,000 tokens per minute**. A 45-second step
pushes roughly 150,000. So the third batch of every step came back `429`.

My first implementation treated any exception as fatal: mark the document
`failed`, store the error, return 502. So a perfectly good document with 197
chunks safely written was declared broken because the provider said "slower".

The fix is a single `except` clause with the opposite meaning:

```python
except TooManyRequestsError:
    logger.warning("Rate limited at chunk %s of %s", written, total)
    break            # end the step politely, keep the progress, stay `processing`
```

Plus a 20-second wait in the browser after a step that made no progress —
because the limit is measured per minute, so calling straight back is pointless.

**Lesson:** classify errors by whether they'll still be true in a minute.
Permanent ones (corrupt file, unsupported type) should fail the document.
Temporary ones (rate limit, network blip) should pause it. Treating all
exceptions alike is what turned a working document into a broken one.

Trial key limits worth remembering: 100k tokens/min, 1,000 calls/month. A
278-chunk PDF is ~222k tokens, so it needs ~3 minutes regardless of how it's
paced. Staying on trial for now.

---

## Part 5 — Frontend

### 16. The loop, and the two guards around it

```tsx
while (!done) {
  const step = await api(`/documents/${id}/ingest/step`, { method: "POST" });
  setProgress(...);
  done = step.done;
  if (!done && step.chunks_done === previous) await wait(20_000);
  previous = step.chunks_done;
}
```

Two `useRef` sets guard it — refs rather than state, because changing them must
*not* trigger a re-render, or updating one mid-loop would restart the effect
that launched the loop:

- `running` — don't drive the same document twice.
- `abandoned` — don't auto-retry one that already errored. Without this, a
  document stuck at `processing` would be retried on every refresh: a loop of
  billable calls nobody asked for.

One rule starts it, covering both cases:

```tsx
documents?.filter(d => d.status === "pending" || d.status === "processing")
          .forEach(d => void ingest(d.id));
```

A fresh upload arrives as `pending`; a document abandoned by a closed tab
arrives as `processing`. `failed` is excluded on purpose — retrying costs
money, so it's a button, not an automatic behaviour.

### 17. BUILD.md's 3-second polling was deleted, not implemented

The plan said to poll `GET /documents` every 3 seconds while anything is
processing. With the step loop, that's asking the server something the browser
learned one line earlier. The step responses *are* the progress.

Nice moment: a design decision made the previous day's plan item unnecessary
rather than harder.

### 18. Making the first step return early

The first version showed nothing for 45 seconds, then jumped to "192 of 278".
It looked frozen, then teleported.

Nothing can be drawn until a step returns `chunks_total`. So the *first* step
now stops after one batch:

```python
if resume_from == 0 or time.monotonic() - started > STEP_BUDGET_SECONDS:
    break
```

One extra HTTP round trip, and feedback appears in ~5 seconds instead of 45.

### 19. The progress visual: a chunk grid

Three designs were tried. A plain progress bar, a page silhouette filling with
ink, and a grid of small tiles — one per chunk — lighting up as each is
embedded. The grid won because it shows *the actual thing*: a document becoming
278 searchable pieces, which is the concept the whole project rests on.

Details that mattered: above 120 chunks one tile stands for several (278
squares makes the row taller than the list); before the first response, 40 dim
tiles pulse as a whole, since the size genuinely isn't known yet; and the newer
half of the filled tiles breathes together, so there's motion even during a
20-second rate-limit pause. A staggered "wave" version was tried and rejected —
the synchronised pulse reads better.

### 20. Refusals became a popup, and `accept` was removed

The old behaviour put "File is too large. The limit is 10MB." in small grey
text under the drop zone — appearing behind the file picker you were looking
at. Now a modal names the file and the real number ("`report.pdf` is 24.3 MB,
over the 10 MB limit") and lists every rule together, so one mistake teaches
the whole set. Server refusals (429, 503) go through the same path.

The `accept` attribute was dropped from the input. It made the OS grey out
every other file type, so a wrong file *couldn't* be picked and the rules were
never learned. The server validates regardless — the browser check has always
been a courtesy, not the boundary (Day 5, §13).

Implementation note: this was built with `<dialog>.showModal()` first, which is
the lazier and more correct choice — free backdrop, focus trap, Escape to
close. It was replaced with a state-rendered overlay for a diagnostic reason:
a popup that fails to appear is indistinguishable from validation that never
ran. The overlay cannot fail that way, which made the remaining bug (§25)
findable.

---

## Part 6 — Things that broke

### 21. Pydantic was printing my secrets into tracebacks

Starting the API without `COHERE_API_KEY` produced this:

```
ValidationError: 4 validation errors for Settings
supabase_url
  Field required [type=missing, input_value={'supabase_url': 'https:/...8cGna1vD4fNw84svJityKO'}]
```

Pydantic includes the settings it *did* load as context. Locally that's a
screen. On Railway it's a stored deployment log, visible to anyone with project
access and forwarded to any log tool attached later. And the moment it fires is
exactly the moment you're stressed and about to paste the log somewhere for
help.

One line in `SettingsConfigDict`:

```python
hide_input_in_errors=True,
```

Verified in the wild afterwards — the same failure now prints `[type=missing]`
with no values at all. Found by accident, worth more than most of what was
planned.

### 22. FastAPI 0.140 changed what `app.routes` contains

Listing routes to confirm the new endpoint registered showed only `/docs` and
`/openapi.json`, plus three objects with no `path`. It looked like the routers
hadn't loaded.

They had. FastAPI 0.140 wraps included routers in `_IncludedRouter` objects
instead of flattening their routes into `app.routes`. The authoritative check is:

```python
app.openapi()["paths"]
```

**Lesson:** when a familiar introspection trick returns something impossible,
suspect the tool changed before suspecting the code.

### 23. A "bug" that was two live edits, mid-save

The upload popup appeared broken: no dialog, no console error, clicks doing
nothing. The dev server log explained it — for a few seconds the component
referenced `Button` before its import existed, which crashed the render, and a
crashed component doesn't respond to anything. Fast Refresh recovered, but the
open tab was still running the crashed instance.

**Lesson:** after an edit that touched imports, hard-reload before believing a
symptom. And read the dev server log — it had the answer while the browser
console had nothing.

### 24. The daily cap accused of a crime it didn't commit

When the popup didn't appear, the suspicion was the 15-document cap silently
blocking uploads. The API log settled it in one line:

```
POST /documents/upload HTTP/1.1" 201 Created
```

Not a 429. Removing the cap would have changed nothing while deleting a guard
that took real thought to build.

**Lesson:** check the log before removing the suspect. Guessing costs a working
feature.

### 25. The file that wouldn't open wasn't our bug at all

Final symptom: a specific >10MB file could be *selected* in the picker but the
**Open button stayed disabled**, so no popup ever appeared.

Nothing in our code can do that. The file input has four attributes — `ref`,
`type`, `className`, `onChange` — and the OS picker has no idea what our size
limit is. A disabled Open button means the browser never hands the file over,
so our validation is never reached.

Confirmed by testing other files, which worked fine. It was that one file
(cloud placeholder or locked), not the application.

**Lesson:** "reproduce with a second input" is the cheapest way to tell "my code
is broken" from "this file is weird". Drag-and-drop is a useful second path
because it bypasses the OS dialog entirely.

---

## Part 7 — Verification

Proven by hand, **before any UI existed**, because a browser loop hides which
request did what:

| Check | What it proved |
|---|---|
| Two `curl` steps across a resume boundary, then SQL | 6 rows, 6 distinct indexes, 0..5 — no duplicates, no gaps |
| `count(*) filter (where embedding is null)` → 0 | No chunk stored without a vector |
| `vector_dims(embedding)` → 1536 | Cohere returned the width the column and HNSW index expect |
| Self-check `assert first == second` | Chunking is deterministic — the assumption resume rests on |
| Full document in the browser → `ready` at 278 | The loop works unattended |
| Corrupt/oversized file → refusal with a reason | Errors surface instead of hanging |
| `documents_created_today()` → `1` | The privileged function reads past RLS and returns only a number |

The order mattered: the hinge was tested with two hand-driven requests before a
single line of frontend code existed.

---

## Gotchas worth remembering

- **A background job cannot outlive the token that authorises it.** If auth is
  per-request and short-lived, long work has to be split into requests — or
  given a credential that doesn't expire, which means giving up the security
  model.
- **Classify errors by whether they'll still be true in a minute.** Rate limits
  pause; corrupt files fail. Treating them alike breaks working documents.
- **Pydantic echoes loaded settings into validation errors.** Set
  `hide_input_in_errors=True` in any project where those settings are secrets.
- **`security definer` + `set search_path`** is the narrow way to read past RLS.
  Never one without the other.
- **Cohere Python SDK:** response field is `embeddings.float_` (trailing
  underscore); `input_type` is required; trial keys are 100k tokens/min and
  1,000 calls/month.
- **FastAPI 0.140:** `app.routes` no longer lists endpoint paths for included
  routers. Use `app.openapi()["paths"]`.
- **Bash-tool working directory drifts** between calls. A relative path that
  worked earlier can 127 later; use absolute paths for anything important.
- **The API reads `.env` relative to the working directory.** It must be started
  from the repo root or every setting appears missing.
- **Branches are labels, not containers.** Moving a commit from `main` to a
  branch was `git switch -c <name>` then `git branch -f main origin/main` —
  the commit never moved, only the labels did. Safe only because nothing had
  been pushed.

---

## Still open, flagged for 6b

- **Images.** Page-region rendering, junk filtering (logos, dividers),
  downscaling, Storage upload, and image vectors in the same space as text.
  Migration 003 already has the columns. (The "~2MP cap" I believed while
  writing this could **not** be found in the installed Cohere SDK. What it does
  document is 20MB combined per request, and no limit on image count. 2MP is a
  self-imposed safety guideline in 6b, not Cohere's rule.)
- **Orphaned image files on delete.** Chunks cascade; Storage objects don't.
  Same class of bug as the Day 5 orphan guard.
- **The modality gap.** In a text-heavy corpus, images may be under-retrieved.
  Measure in Day 11's eval; the escalation is embedding image + caption
  interleaved into one vector. Do not build it up front.
- **Day 7 must use `input_type="search_query"`.** Silent quality loss otherwise.
- **Day 7's model trio needs verifying** against provider docs — all three must
  be vision-capable once images can be retrieved.

**What this achieves overall:** documents stop being inert files and become
searchable meaning. A PDF now goes from Storage through parsing, chunking and
embedding into 278 rows of 1536-dimension vectors, scoped by RLS to one user,
resumable if interrupted, rate-limited so it can't empty my wallet, and visible
as it happens. Day 7's chat has something to retrieve for the first time —
everything after this is asking questions of what was built today.
