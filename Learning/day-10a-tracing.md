# Day 10a — Making the Pipeline Visible

Personal learning log. Not read by Claude automatically — this is for me, to
recall what I built and why once the project is done. Not needed as context for
future sessions or development; it's a record, not a spec.

Every day up to now added something the user can see. Today added something
**I** can see.

Back on Day 7 I found that a vague question retrieved chunks scoring 0.18–0.25
while a near-verbatim quote scored 0.68, and that four of the top five chunks
were images. I found that by hand, once, for one question. There was no way to
see it happening on any other question — an answer either arrived or it didn't,
and when it was bad, "why" was a matter of guesswork and Railway logs.

A chat request is nine blocking calls in a row. When the answer is wrong, six of
them could be the culprit.

Day 10a is the day the app started showing its working.

---

## Part 0 — What tracing actually is, and the trap in it

**A trace is one request. A span is one step inside it.** LangSmith records each
step with its real inputs, outputs and timing, nests them into a tree, and ships
it to a dashboard I can click into.

The trap is specific to this kind of feature and I want it written down:

> **Tracing fails silently by design.** Every other feature in this app tells me
> when it breaks — a 500, an error event, a red banner. Tracing that is wired up
> wrong produces an *empty dashboard*, or worse, a dashboard that looks fine and
> is subtly lying about the shape of the request.

That's why this day was mostly *verification*, and why the first thing built was
a throwaway script rather than a feature.

---

## Part 1 — The hinge: an invisible variable that can't cross a thread

This is the load-bearing idea of the whole day.

LangSmith normally links a child span to its parent through a **contextvar** —
an invisible per-thread variable it sets when a trace opens, and reads when a
child starts. Ninety-nine percent of the time you never think about it. Decorate
your functions, get a tree.

It cannot work here, and the reason is the shape of `/chat`:

```
POST /chat
    |
    |  chat() runs as a SYNC handler in a FastAPI worker thread
    |  (deliberate, since Day 7 — the calls block)
    v
  history -> rewrite -> embed -> retrieve -> load_images -> build_messages
    |
    |  ...then chat() RETURNS and exits. The model has not been called yet.
    v
  generate()  <- a generator, pulled one chunk at a time by Starlette,
                 each pull a SEPARATE hop into the thread pool with a
                 FRESH COPY of the context
```

Two boundaries, and contextvars survive neither:

- a worker thread gets a *copy* of the context going in, and nothing comes back out
- the streaming generator is resumed on a different thread per chunk, each with a
  fresh copy — so something set during chunk 1 is gone by chunk 2

So decorating the functions and hoping produces **eight unrelated traces per
question**, not one tree. The fix is to create the root run by hand, hold it in
an **ordinary Python variable**, and re-establish it as the parent where needed.
Plain variables cross threads fine. Invisible ones don't.

That much was proven in an earlier session with a spike script. What I didn't
expect was that the plan built on top of it would still be wrong twice.

---

## Part 2 — Two bugs in my own plan, and the difference between arguing and measuring

Both of these would have shipped without an error. Both produce a dashboard that
is quietly wrong, which per Part 0 is the worst outcome available.

### Bug 1 — the root run sets nothing

`start_root` builds the root and returns it. That's all it does. It does **not**
touch the contextvar the `@traceable` decorators read.

So the six pre-flight steps — history, rewrite, embed, retrieve, images, prompt
— would each have opened *their own top-level trace*. The plan's step 6 only
wrapped that block in a try/except to close the root on failure. It never said
to adopt the root as parent.

### Bug 2 — the right code in the wrong place

The plan said: wrap the whole of `generate()` in the "adopt this parent" block.

That doesn't work, and Part 1 is why. `generate()` sends two events before the
streaming loop starts:

```python
with adopt(root):          # <- entered here, during chunk 1
    yield conversation     # <- pause. resumed on another thread, fresh context
    yield sources          # <- pause. again
    for text in stream():  # <- by now the block has been forgotten
```

The block is entered during the first chunk and **forgotten by the time the loop
begins**, so the answer-streaming span would have floated off on its own anyway.

It has to sit directly around the loop:

```python
yield conversation
yield sources
with adopt(root):              # <- entered
    for text in stream():      # <- and the FIRST pull happens right here,
        yield token            #    in the same hop. That first pull is the
                               #    moment the span decides who its parent is.
```

### The bit I want to remember

I could have written a paragraph explaining why I was right. Instead: a ~60-line
script that builds **both** versions and runs each through Starlette's actual
chunk-pulling machinery, printing the parent each span ended up with.

```
A (plan: with at top)          parent=None       ORPHANED
B (shipped: with around loop)  parent=019ffda3…  ATTACHED
```

Two lines of output settled it, cost nothing, and uploaded nothing. **The
general rule it produced**, which is worth more than the fix:

> A traced generator picks its parent at its **first** `next()`. So the block
> that establishes the parent and that first pull have to land in the same
> execution slice.

---

## Part 3 — What it caught in the first hour

The point of an instrument is what it shows you that you weren't looking for.

### One trace, seven spans, zero detached

```
root: chat_query   status=success
  - embed_query      embedding    7.70s
  - retrieve         retriever    0.36s
  - load_images      tool         1.79s
  - build_messages   prompt       0.00s
  - stream_answer    llm          0.97s
  - generate_title   llm          0.54s

runs: 7   detached: 0
similarities: [0.208, 0.186, 0.183, 0.176, 0.171]
```

`detached: 0` is the line that says both fixes from Part 2 were necessary and
both worked. Under the plan's version it would have read `detached: 6`.

### Day 9b became provable

Yesterday's rewrite was invisible — never sent to the model, never saved, never
shown, only logged. Now it's a span, and the numbers are the argument:

| | question searched | top similarities |
|---|---|---|
| first question | `What is this document about?` | 0.208, 0.186, 0.183 |
| follow-up | `"give more detail"` → *"Provide more detail about the project report completed by the students from the Department of Mechanical Engineering…"* | **0.599, 0.573, 0.512** |

Day 9b's log ended with "the chunks looked right to me". This is the same claim
with a number under it.

### A bug I didn't know I had

```
WARNING: Could not load image chunk .../img-157.jpg; answering without it
WARNING: ...img-136.jpg
WARNING: ...img-156.jpg
WARNING: ...img-149.jpg
```

**All four images behind that answer failed to download.** The answer went out
looking completely normal — grounded, cited, confident — built from the
acknowledgement page alone. Day 6b may as well not have happened for that
question, and nothing on screen said so.

I could not reproduce it: the files exist, the paths are right, RLS permits them,
a direct download works, and both a later request and a fresh cold process loaded
all four fine. So I don't know the cause and I'm not going to invent one.

What I did instead is make it impossible to hide again — the warning now carries
the exception class and message. The original code deliberately logged neither,
on the reasoning that a storage client can echo request headers into an error.
That instinct was right; the implementation threw out the useful half with the
dangerous half.

> **A swallowed error is a bug that gets to keep happening.**

### A number Day 11 needs

`embed_query` took **7.70s** on the first call of a cold process and
**0.11–0.42s** on every call after. Day 7 measured the whole pre-model pipeline
at 1.72s. So the first question after any deploy is an outlier by a factor of
twenty, and Day 11 must not read it as typical.

---

## Part 4 — The check that failed, and my own claim I had to withdraw

The plan had twelve verification rows. Two of them were flagged as "the ones a
happy-path demo never catches": what happens on the zero-documents path, and what
happens if you **close the browser mid-answer**.

I wrote a `finally` block to close the trace on that path, and I told myself —
and wrote in the code — that it also closed a gap flagged back on Day 8, where a
disconnect means the exchange is never saved.

Then I tested it. curl, cut off at five seconds. The trace:

```
status=pending   error=None   answer=None
- stream_answer   llm   running
```

Still `running` minutes later. **The `finally` never ran.**

Reading Starlette's source rather than guessing:

```python
async def iterate_in_threadpool(iterator):
    as_iterator = iter(iterator)
    while True:
        try:
            yield await anyio.to_thread.run_sync(_next, as_iterator)
        except _StopIteration:
            break
```

There is no `finally` in there. Starlette **pulls** the generator but never
**closes** it. On a disconnect the whole thing is abandoned mid-`yield`, nothing
throws `GeneratorExit` in, and a `finally` inside the generator is simply never
entered.

One cause, three symptoms:

1. the exchange is never saved (known since Day 8)
2. the trace stays "running" forever, which reads as a hung request
3. **the model stream stays open and billable** until the garbage collector
   happens to get to it

The third one I hadn't considered at all, and it's the one that costs money.

I chose to defer the fix to 10b rather than half-solve it — the proper fix lives
outside that function (an ASGI middleware, or a wrapper that owns closing the
generator) and it's the *same* fix the unsaved-exchange decision needs. But I
corrected the comments immediately, because a comment asserting something I'd
measured to be false is worse than no comment.

> **"I reasoned it, therefore it works" is how you end up documenting a bug as a
> feature.**

---

## Part 5 — The id that was in two places

A public LangSmith trace is a genuinely good portfolio artefact: one link, whole
pipeline, real timings, no signup. But public means public.

I asked for my Clerk user id to be stripped from the shared trace. It was in the
root metadata, which was the obvious place — and it was also the **first segment
of every image path**:

```
user_3HFRkTSnTzAZVJt743w1wlJjYp2/d944aa73-.../img-157.jpg
```

Those paths ride inside the `retrieve` span's output, which wasn't redacted at
all. Stripping only the metadata would have left it plainly visible while
*looking* solved — which is the same failure mode as Part 0, dressed differently.

The fix went in at the source rather than as a ritual before each share:

- metadata carries a short one-way hash (`67231d8f44d5`), still stable enough to
  group one user's traces and still findable by hashing the id I'm hunting
- storage paths get that one segment swapped for `<user>`, keeping the rest,
  because *which figure was chosen* is the useful part

Two things I noted rather than fixed. **Traces made before this still contain the
raw id** — so the one I share has to be a new one. And the raw id was never the
most revealing thing in there anyway: a public trace shows the document's *name*
and the retrieved text, and my document is called
`major project-LAST2.3nishanth updated.pdf`.

---

## Part 6 — Feedback, and one decision that looks like a detail

I asked for a way to collect feedback. 👍/👎 under each answer, plus an optional
comment.

The obvious design is a new table. I didn't build one, and the reason is the
whole point of the day: **a score on its own tells me an answer was bad. A score
attached to its own trace tells me the retrieval scored 0.18 and four of the five
chunks were images.** So feedback is written onto the LangSmith run — no table,
no migration, no join to maintain.

### The detail worth remembering

The thumb and the comment go in under **two different keys**.

My first instinct was one record carrying both. That's wrong in a way that only
shows up later: LangSmith groups feedback into a column by key and lets you
average it. If comments shared the rating's key, every person who bothered to
explain themselves would add a **score-less row** to the column I average — and
"how are the answers doing?" would slowly stop being answerable by the one
operation that made it easy.

Same reason the browser sends the thumb on click and any comment as a separate
submission carrying no score: repeating it would count one person twice.

> **Decide what question the data has to answer, then choose the shape. Not the
> other way round.**

The ordering matters too: the thumb is sent the *instant* it's clicked, before
the comment box appears. Almost nobody writes a sentence. Asking for one first
trades the signal most people give for the one most people won't.

---

## Part 7 — The migration that turned trust into proof

I shipped that with a limitation I wrote down honestly: run ids only lived in the
browser's memory, so **reloading a conversation made its answers unratable**.

Which is backwards. The answer I want to complain about is usually the one I came
back to.

So: migration 006, one nullable column, `messages.run_id`.

Design notes I want to keep:

- **Nullable forever.** User rows have no run (a question isn't something a
  pipeline produced), answers predating the migration have none, and answers
  produced with tracing switched off have none. `not null` would make an
  observability vendor a hard dependency of saving a message — exactly the
  coupling the rest of the day avoided.
- **No foreign key.** It points into another company's database. Postgres can't
  check it and shouldn't pretend to.
- **Partial index**, `where run_id is not null`, because half the table can never
  match the only query that reads it.

### The part I didn't expect

Storing it did something better than the convenience I asked for. Before, the
feedback endpoint took `run_id` **on trust** — anyone could post a rating for any
run id. Now the rating is checked against a `messages` row, and
`messages_isolation` from `001_init.sql` scopes that select to the caller. So a
run belonging to someone else and a run I invented get the **same 404**, and the
check needs no `where user_id` of its own.

That's this project's central rule paying off again: **RLS is the security
boundary, not backend filtering.** The endpoint doesn't know who I am. It doesn't
need to.

```
3. rating my own run (expect 204):      HTTP 204
4. rating a run that is not mine:       HTTP 404
```

### The deploy hazard hiding inside it

The insert now includes `run_id`. Against a database without that column, the
insert **fails** — and `_save_exchange` swallows its own failures by design. So
shipping the code before the migration would mean **answers silently stop being
saved** while the app looks perfectly healthy.

Applying the migration first closed it. But this is a general shape worth
recognising: *a deliberate swallow plus a schema change is a silent-failure
machine.* The swallow is still right; the ordering is what has to be respected.

---

## Part 8 — Small things that cost time

- **The docs are in `node_modules`.** `apps/web/AGENTS.md` says this Next.js
  (16.2.12, React 19.2.4) has breaking changes and its bundled docs must be read
  first. A fresh worktree has no `node_modules` — it's gitignored — so `npm ci`
  comes before reading anything or typechecking anything.
- **The typechecker caught a real mismatch.** I made `score` required in the
  frontend's request type while the backend had made it optional. `tsc --noEmit`
  found it in two seconds. That's the value of mirroring the API's shape in
  `types.ts` instead of using `any`.
- **A deprecation warning is a future outage.** The SDK warned that filing
  feedback without a `session_id` "will stop working in a future release". Fixed
  the same day with one cached lookup. Warnings in a log are the cheapest bug
  reports I will ever get.
- **Tokens live ~60 seconds and I kept losing them.** My first verification
  script did three model calls behind one token; the first call ate the budget
  and calls two and three came back `{"detail":"Token has expired."}`. Splitting
  it into short scripts fixed it. Cheaper still: I read most results back out of
  LangSmith with its own API, which needs no Clerk token at all.

---

## What Day 10a cost

Eleven commits. One new service module, eight decorators, ~40 lines in the chat
endpoint, one new endpoint, one small React component, one migration. No new
dependency — `langsmith` was already installed transitively, and got declared
explicitly so a transitive dependency dropping it can't silently kill tracing.

Everything is optional at runtime: with `LANGSMITH_TRACING` unset the app boots,
answers, and sends nothing. A portfolio demo must not die because an
observability vendor is unreachable.

## Still open

- **Row 10 fails and is deferred to 10b** — client disconnect leaves the trace
  open, the exchange unsaved, and the model stream billable. One fix, three
  symptoms.
- **The image-download failure is unexplained.** Not reproducible, now
  diagnosable. If it recurs on Railway, Day 11's retrieval numbers are measuring
  a pipeline that sometimes drops its pictures.
- **Row 9, the zero-documents path, is untested** — it needs an account with
  nothing uploaded.
- **Nothing is verified on Railway yet.** "Works locally" has already fooled this
  project once, back on Day 7 with `Path(...).parents[3]`.
- **The public trace URL for the README** is the last step, and it has to come
  from a trace created *after* the anonymisation in Part 5.
- Cross-user RLS on retrieval is *still* untested. Inherited from Day 7 and
  carried through Days 8, 9a, 9b and now 10a. Day 11 settles it.
