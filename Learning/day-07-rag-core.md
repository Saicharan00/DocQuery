# Day 7 — The App Finally Answers

Personal learning log. Not read by Claude automatically — this is for me, to
recall what I built and why once the project is done. Not needed as context for
future sessions or development; it's a record, not a spec.

Six days built a filing cabinet: upload a document, split it, turn every piece
into numbers, store it. Nothing read any of it back. Day 7 closes the loop — a
question becomes numbers, those numbers find the closest pieces, the pieces
become a prompt, the prompt streams an answer, and the exchange is saved with its
citations.

---

## The idea in one paragraph

A question and a passage that answers it mean roughly the same thing, so their
embeddings sit close together. "Find the answer" therefore becomes "find the
nearest vectors" — arithmetic, not comprehension. The model never searches. It is
handed five passages and told to answer using only those, which is what makes the
answer checkable: every claim traces to a numbered source, and when the sources
don't contain the answer the model is instructed to say so rather than fall back
on what it happens to know.

That last part is the whole difference between this and a chatbot. A chatbot
sounds confident about everything. This is designed to be able to say "not in
your documents."

---

## Part 0 — The shape of the day

Seven steps, in dependency order. Each one had to work before the next was worth
writing.

| Step | What | Why it came here |
|---|---|---|
| B1 | Migration `005` | No vector search exists at all without it |
| B2 | **Prove the RPC** | If a vector can't cross into Postgres, everything after is written against a broken assumption |
| B3 | Config + keys | Nothing can authenticate |
| B4 | `services/rag.py` | The pipeline, importable with no web server in it |
| B5 | `models/chat.py` | Request validation, including the model allowlist |
| B6 | `routers/chat.py` | The endpoint, SSE, spend cap, saving |
| B7 | Deploy | Streaming only means something behind a real proxy |

B2 is the one worth pointing at. It was a throwaway script whose only job was to
answer "does a Python list of 1536 floats survive the trip into a `vector(1536)`
column?" before a single line of application code depended on the answer. It did.
Had it not, the fix would have been a one-line change in one place instead of a
rewrite of four files.

---

## Part 1 — Why the search had to be written in SQL

Every other query in this app goes through PostgREST — the layer that turns
`supabase.table("chunks").select(...)` into SQL. It's convenient and it covers
almost everything.

It cannot express this:

```sql
order by embedding <=> query_embedding
```

`<=>` is pgvector's cosine **distance** operator, and PostgREST's query language
has no syntax for it. So the single most important query in the entire project is
the one query that can't be written the normal way. It lives in migration `005`
as a database function, called with `.rpc("match_chunks", ...)`.

### The security decision that looks like a typo

```sql
create function public.match_chunks(...)
language sql
stable
as $$ ... $$;
```

No `security definer`. Migration `004` has it, and this one deliberately doesn't,
and getting that backwards would be the worst bug in the project.

- `documents_created_today()` in `004` **must** see past row-level security — it
  counts everybody's uploads to enforce a service-wide cap. It cannot do its job
  as the calling user.
- `match_chunks` is the exact opposite. RLS is precisely the thing stopping one
  user's question from searching another user's documents. Leaving it as the
  default (`security invoker`) means the function body runs as whoever called it,
  so the `chunks_isolation` policy still applies **inside** the function.

Add `security definer` here and the app would keep working perfectly, look
identical, and quietly let anyone search everyone's documents.

**Lesson:** the dangerous security bugs don't throw errors. They return results.

### `1 - distance`

The function returns `1 - (embedding <=> query_embedding)` as `similarity`,
because "0.87 similar" is something a human can reason about and "0.13 distant"
isn't. Same number, readable direction.

---

## Part 2 — The one-word difference that fails silently

```python
def embed_query(question: str) -> list[float]:
    return ingestion.embed([question], input_type="search_query")[0]
```

This whole function exists for `input_type="search_query"`.

Cohere embeds a stored passage and a search query **differently** — a passage is
being catalogued, a question is doing the looking, and the model is trained to
account for that asymmetry. Ingestion used the default, `search_document`.

Passing the same default here would raise no error, log nothing, and return
answers that are simply a bit worse than they should be. Forever. A wrapper
function around one line is usually pointless; this one exists to make a silent
failure impossible to introduce by accident.

**Lesson:** when a wrong value produces no error, put it somewhere it can only be
written once.

---

## Part 3 — Sending pictures to a model

Day 6b stored figures as chunks whose `content` is only the label
`[Image from page 74]`. Retrieval can return one, but sending that label to the
model tells it nothing — it would confidently answer from a caption.

So a retrieved image chunk gets its JPEG pulled back out of Storage and converted
into a **data URI**: the picture rewritten as one very long line of text, which is
the only form that fits inside a JSON message.

```python
images[path] = f"data:image/jpeg;base64,{base64.b64encode(jpeg).decode()}"
```

The prompt then mixes two kinds of part in one message — a text header naming the
source, then the picture itself:

```python
parts.append({"type": "text", "text": f"{label} (image):"})
parts.append({"type": "image_url", "image_url": {"url": data_uri}})
```

The header is what lets the model write "[3]" about something it *saw* rather
than read.

A download that fails is logged and skipped, not raised. One unreachable figure
should cost that figure, not the whole answer — the four text chunks retrieved
alongside it are still worth sending.

### It worked, and here's how I know

The first real question was "what is this document about?" against a 77-page GPS
report. Four of the five retrieved chunks were images. The model replied that the
sources showed "specific sections and **diagrams**... and a diagram with numbered
labels."

Nothing in the text it received said "diagram". It read the JPEG.

---

## Part 4 — Streaming, and the header that makes it real

An answer takes several seconds to write. Waiting for the finished paragraph
means several seconds of a blank screen, which reads as a hung app. So the answer
is sent as **Server-Sent Events**: a long-lived response with pieces pushed down
it as they arrive.

```
event: conversation   → the id, first, so a new chat knows where it lives
event: sources        → the citations, before any text, so they can render early
event: token × N      → the answer, piece by piece
event: done
```

Two details that took thought:

**The payload is JSON, not raw text.** SSE is line-based — a bare newline inside
`data:` would be read as the end of the message. Answers contain newlines
constantly. JSON turns them into the two characters `\n`, which is what makes
streaming prose safe at all.

**`X-Accel-Buffering: no`.** Nginx-family proxies buffer responses by default.
Without this header, Railway would hold every token until the answer finished and
deliver it in one lump — a long pause followed by a wall of text, indistinguishable
from no streaming whatsoever. The code would be perfect and the feature would be
dead.

### Errors have a deadline

The handler does everything that can fail *before* the stream opens:

```
spend cap → embed → retrieve → no chunks? 400 → load images
          → create/verify conversation → THEN StreamingResponse
```

Once the first byte is sent, the HTTP status line has already gone out as
`200 OK`. A failure after that point can't be a 500 — it can only be an SSE
`error` event that the browser must be written to understand. So the window in
which that's possible is kept as small as it can be.

**Lesson:** in a streaming response, the status code is spent before the work
starts. Order your failures accordingly.

---

## Part 5 — The allowlist is a money guard

```python
ModelName = Literal["gemini/gemini-3.5-flash-lite", "gpt-5.4-nano"]
```

This looks like typing pedantry. It's a spending control. Without it a caller
puts any model name they like in the request body, LiteLLM cheerfully routes to
it, and the bill is mine. The `Literal` makes an unknown model a 422 rejected by
validation before my code runs at all.

Same reasoning for `message: str = Field(min_length=1, max_length=2000)`. The
floor stops an empty question costing an embedding call; the ceiling stops
someone pasting a book.

And `k` — how many chunks to retrieve — is deliberately **not** in the request.
A caller choosing `k` is a caller choosing how much of my money to spend per
question.

---

## Part 6 — Three things that went wrong, which is where the day's value is

### 1. The default model was dead, and the docs said otherwise

First real call to `gemini-2.5-flash-lite`:

```
404: "This model is no longer available to new users."
```

Its published shutdown date was still two months away. Google's own catalogue
endpoint **still listed it**. Access had simply been closed to API keys created
after some earlier cutoff — and my key was new.

**Lesson, and it generalises past this one API:** being listed in a provider's
catalogue is not the same as being callable with your key. Only an actual request
distinguishes them. Test each provider with a throwaway call before wiring
anything to it.

The replacement is `gemini-3.5-flash-lite` ($0.30/$2.50 vs the old $0.10/$0.40).
I chose it over the cheaper `gemini-3.1-flash-lite` because at the 500-question
daily cap the difference is about 25¢ a day, and the newer generation is furthest
from meeting the same fate. I rejected `gemini-flash-lite-latest` — an alias that
always points at the newest model — because the model underneath can change
without warning, and Day 11 compares eval runs against each other. A default that
silently becomes a different model mid-evaluation makes those numbers
incomparable.

### 2. A crash that could only happen in production

The deploy failed. The log:

```
File "/app/app/config.py", line 16, in <module>
    ENV_FILE = Path(__file__).resolve().parents[3] / ".env"
IndexError: 3
```

`parents` is the list of folders above a file. Locally `config.py` is at
`E:\DocQuery\apps\api\app\config.py`, so `[3]` is the repo root — correct. On
Railway the service root is `apps/api`, so the file deploys to
`/app/app/config.py` and counting up reaches the filesystem root at `[2]`. There
is no `[3]`.

The module never finished importing, so uvicorn couldn't load the app, so nothing
answered `/health`, so the deploy was marked failed. The fix stops counting and
starts searching:

```python
ENV_FILE = next(
    (p / ".env" for p in Path(__file__).resolve().parents if (p / ".env").is_file()),
    None,
)
```

`None` when nothing is found is the *right* answer on Railway — it injects real
environment variables and there is no file to read.

The bug had been sitting on the branch since before Day 7 started. It surfaced
now only because Day 7 was the first deploy after that commit.

**Lesson:** counting directory levels encodes an assumption about a layout that
differs between machines. Searching for the file cannot go wrong the same way.

### 3. I measured the right thing the wrong way

My first streaming test compared the first token to the last, and printed
`BUFFERED` when they were 0.11s apart.

Wrong test. That gap mostly measures how coarsely the *provider* chops its
stream — Gemini sent a short answer in five near-simultaneous lumps, which looks
identical to a buffered response. What buffering actually does is collapse the
**entire** event sequence to one instant: `conversation`, `sources`, every token
and `done` all released together.

The honest measure is the span from first event to last. The real numbers:

```
1.72s  sources
3.66s  first token      ← a 1.94s gap no buffering proxy can produce
4.89s  done
```

**Lesson:** a test that can't distinguish two different causes isn't a test. When
a result surprises you, check the measurement before believing it.

---

## Part 7 — Numbers worth remembering

**Time to first word: 3.66 seconds**, and the split is the interesting part.
1.72s of it is *my* pipeline before the model is even called — spend-cap check,
Cohere embedding, vector search, image downloads from Storage, conversation
insert, each a separate network round trip. The remaining 1.94s is the model
thinking. The half I control is as big as the half I don't.

**128 tokens in 0.61 seconds** once it starts. The answer doesn't trickle; it
arrives. Day 8's UI shouldn't assume a leisurely typewriter effect.

**Similarity scores of 0.18–0.25**, where a near-verbatim quote scored 0.68 on
Day 6. A vague question ("what is this document about?") retrieves weakly, and
image chunks — whose only text is a page label — dominated the top five. That's
real data for Day 11.5's abstention threshold, which is supposed to say "I don't
have this" below some cutoff. Setting that number by intuition would have been
guessing.

**Streaming granularity differs by provider**: 4-5 pieces from Gemini, 74-128
from OpenAI, for comparable answers. Same code, same SSE format.

---

## Part 8 — Testing against a 60-second credential

Retrieval is scoped by row-level security, which is driven by the Clerk token on
the request. So there's no way to test it without a real token — and those live
60 seconds.

The first two attempts got `401`. Measuring rather than guessing showed why: the
token had **expired three seconds before it even reached the file**. Copy from
browser, paste into a terminal, tell Claude, Claude runs curl — that round trip
takes longer than the credential lasts.

What works, and keeps the token out of the chat log entirely:

1. Terminal: paste the command, **don't** press Enter.
2. Browser console: `copy(await window.Clerk.session.getToken())` — prints
   `undefined`, which is correct; `copy()` returns nothing and puts the token on
   the clipboard as a side effect.
3. Terminal: Enter. The command reads `(Get-Clipboard).Trim()` into a variable.

Elapsed: about five seconds instead of sixty.

Two smaller Windows lessons from the same afternoon: PowerShell mangles inline
JSON on its way to `curl.exe`, so put the body in a file and use `-d "@body.json"`.
And `Out-File`/`Tee-Object` write UTF-16, which makes saved output look
l-e-t-t-e-r-s-p-a-c-e-d — harmless, but alarming the first time.

---

## What Day 7 deliberately did *not* build

- **Conversation history.** Every question is single-turn. History *and* the query
  rewriting it needs move to Day 9 — not to save time, but because the feature is
  worth more once Day 11 can measure the failure it fixes.
- **Hybrid search / re-ranking / abstention.** Day 11.5, after there's a baseline
  to compare against. Building them first produces unfalsifiable bullet points.
- **A cross-user RLS test.** The one "done when" box still unticked. Needs a
  second Clerk account; Day 11 automates it.

---

## The one-line summary

The retrieval is arithmetic and the generation is rented — the engineering is in
the boring parts: which `input_type` string you pass, whether a SQL function is
`security definer`, whether a header survives a proxy, and whether you tested the
thing you think you tested.
