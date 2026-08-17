# Day 9c — Four Bugs That Never Threw an Error

Personal learning log. Not read by Claude automatically — this is for me, to
recall what I built and why once the project is done. Not needed as context for
future sessions or development; it's a record, not a spec.

This day wasn't planned. I opened the session to ask whether we had semantic
caching, ended up learning how to use subagents, and closed it having fixed four
bugs that had been sitting in the app for weeks.

Every one of them shares a property, and it took me until the end of the day to
notice it:

> **Not one of them produced an error. Not a crash, not a red log line, not a
> failed request. The app reported success every time.**

Day 9b had already taught me one version of this — "give count" returned HTTP 200
and a worthless answer. I thought that was a one-off. It isn't. It's a category.

---

## Part 0 — The question that started it

I asked whether the app had semantic caching. It doesn't, and it's in BUILD.md's
"deliberately not built" list.

What I actually learned came from the pushback. Semantic caching means: embed the
incoming question, and if it's close to one asked before, replay the old answer
instead of paying for retrieval and generation. Sounds like free money.

It isn't, for this app, because **an answer is only valid for one user's document
set**. The same question over different documents has different correct answers.
A global cache doesn't just miss — it serves someone else's answer and calls it
yours. Scoped per conversation the document set is fixed, which is the thing that
makes a hit safe.

So it moved from a one-line dismissal into its own "What's next" entry with the
reasoning written down. Naming the gap and the reason beats listing it as
future work I never justified.

---

## Part 1 — Subagents, and the lesson that isn't the syntax

Day 10 is two halves: LangSmith tracing, and an error-handling sweep. They looked
independent, so I asked to run them as two parallel agents — mostly because I
wanted to learn the tool.

They are independent as *ideas*. They are not independent as *files*:

| File | Tracing | Error handling |
|---|---|---|
| `services/rag.py` | ✅ | ✅ |
| `routers/chat.py` | ✅ | ✅ |
| `routers/documents.py` | ❌ | ✅ |
| 5 `.tsx` files | ❌ | ✅ |

Two agents editing `rag.py` at the same time don't merge. The second write wins
and the first agent's edits vanish — silently, with neither agent aware. I'd have
found out on Day 11 when half the traces were missing.

> **You parallelize by file ownership, not by topic.**

That's the real lesson and it's worth more than the tool syntax. The syntax I can
look up.

### What a subagent actually is

A fresh Claude with its own context window. It **cannot see the conversation I'm
having**. It reads CLAUDE.md, works from the prompt it's handed, and reports back
at the end. No progress updates — the parent doesn't supervise the child, it waits
for the postcard.

Two consequences I hadn't appreciated:

- The prompt has to carry *everything*. A vague prompt burns a whole context
  window producing something I throw away.
- It costs **more** tokens than just doing the work, not fewer. Two cold agents
  re-derive context the main session already has.

So the reason to reach for one is genuinely-separable bulk reading, or wanting to
keep working while something tedious happens. Not speed by default.

### The easier on-ramp I should have started with

Instead of two editing agents in parallel, I ran **one read-only agent**. The
`Explore` type has no edit tools — it physically cannot touch the repo. Zero risk,
and it teaches the whole model: cold start, works alone, reports back.

The task was real Day 10 work, not a toy: inventory every error-handling site in
the app and check four specific failure classes for coverage. It read all 17 Python
and 10 TypeScript files in about two and a half minutes while I carried on working.

### The prompt design bit worth keeping

The two lines that made the report useful rather than noise:

```
Skip: validation handled declaratively by Pydantic/FastAPI, and re-raises
that a caller clearly handles. I want real gaps, not a grep dump.
```

and Part 2 of the prompt — an explicit checklist of four failure classes to
confirm or declare MISSING.

**An inventory of what exists can never tell you what's absent.** Grep finds
handlers. Only a checklist finds the handler that was never written. That single
insight is why the report was worth having.

---

## Part 2 — Verify the agent, in both directions

The agent came back confident. It was mostly right. But "mostly" is doing work in
that sentence, and I only know which parts because we checked.

**Check 1 — its biggest claim.** It said there was no timeout on any LLM call
anywhere. Grepped it: three `litellm.completion` calls, zero `timeout=`. True.

**Check 2 — a claim I doubted.** It ranked "a failed image download means the
model cites a source it never saw" as a runner-up. I thought that was too low —
fabricated citations are exactly what Day 11 measures. So we looked.

I was wrong. `load_images` documents the degradation deliberately: one unreachable
figure costs that figure, not the whole answer, and the model is still told the
source is an image. The agent had ranked it correctly.

That's the part I want to remember. **Verification isn't only for catching an
agent being wrong — it's also for catching me being wrong about the agent.** I
went in expecting to promote that bug and came out agreeing with the ranking.

---

## Part 3 — The timeout that was a hundred minutes

`litellm.completion` was being called with no `timeout`. I assumed "no timeout"
meant some sensible library default.

```
litellm.request_timeout = 6000.0
```

**Six thousand seconds. A hundred minutes.** A provider that accepted the
connection and then went quiet would have held a Railway worker for an hour and a
half.

There was partial cover on the answer path — the browser gives up after 20 seconds
of silence (Day 8's idle guard). But that guard counts from the last token, and
**the two helper calls aren't streamed**, so there's no last token to count from.
`rewrite_query` and `generate_title` were completely unbounded.

The fix is two constants:

```python
HELPER_TIMEOUT = 10   # rewrite_query, generate_title
ANSWER_TIMEOUT = 60   # stream_answer
```

Two numbers, not three, and the reasoning is the interesting part: the two helpers
are the same shape of call — one short sentence from the cheap model, with a caller
already holding a fallback. Something optional should give up fast. The answer has
no fallback and is the thing I'm sitting there watching, so it gets to be patient.

### The check that mattered before writing it

Before adding `timeout=` I checked that a new exception type had somewhere to
land. All three callers already catch bare `Exception` and degrade
(`chat.py:255`, `chat.py:334`, `chat.py:400`), and `litellm.Timeout` is an
`Exception`, so a timeout degrades exactly like the failures those handlers were
written for.

**Turning a hang into an unhandled 500 would have been a worse bug than the one I
was fixing.** Checking the callers first is cheaper than finding that out live.

And separately: `inspect.signature(litellm.completion)` to confirm `timeout` is a
real parameter. A kwarg that doesn't exist gets swallowed into `**kwargs` and does
nothing — which would have looked exactly like a fix.

---

## Part 4 — Library errors going into my database

`documents.py` did this on any ingestion failure:

```python
message = str(exc) or exc.__class__.__name__
```

That string went to the user **and** into the `documents.error` column, which
`document-list.tsx:207` renders verbatim. So a corrupt PDF showed the user
pymupdf's `syntax error: cannot find startxref`, and that text was **stored
permanently**.

The fix turned out to be four lines, because of a property of the codebase I
hadn't noticed: every deliberate user-facing error in this path is a `ValueError`.
`ingestion.parse` on an unsupported type, the empty-file check, and now the new
encoding refusal. Everything else — pymupdf, Cohere, the storage client — raises
something else.

```python
if isinstance(exc, ValueError):
    message = str(exc) or "This file could not be read."
else:
    message = "Something went wrong while reading this file. ..."
```

The general shape: **what lands in a column the user reads is a decision, not
whatever the nearest dependency happened to phrase.**

---

## Part 5 — The encoding bug, which is the nastiest one here

`_parse_txt` was one line:

```python
return [(1, data.decode("utf-8", errors="replace"))]
```

The comment defending it was reasonable: one bad byte shouldn't cost the user
their whole upload. Fine for one bad byte. Not fine for a file that isn't UTF-8 at
all, which becomes a page of `���`, gets **embedded**, and stored.

No error. No warning. The upload says "ready". The damage shows up weeks later as
retrieval that inexplicably misses, and I'd have been debugging the retriever.

### The trap inside the fix

My first instinct was: count the replacement characters, refuse if there are too
many. That catches most of it.

It does **not** catch UTF-16 — which is what Notepad writes when you pick
"Unicode", one menu click away on this machine. UTF-16 text decoded as UTF-8
doesn't produce `�` at all. Every second byte is a NUL, and NUL is a perfectly
legal character. It sails straight through a ratio check.

So the order matters:

1. UTF-16 by byte-order mark — the one encoding that announces itself
2. strict UTF-8 — either works or raises, no ambiguity
3. loose decode, then refuse if more than 10% is unreadable

**Identify what can be identified for certain first. Guess last.**

I deliberately left out a cp1252 fallback. cp1252 decodes almost *any* byte
sequence successfully, so adding it would rescue some real files and silently
accept binary garbage as text. Refusing with "re-save this as UTF-8" is a worse
outcome for one user and a better outcome for the database.

### The five cases I checked

Ordinary UTF-8 · UTF-8 with a BOM and accents · **UTF-16 with a BOM** · one stray
byte in a long file · a wholly mis-encoded file. All five pass, and case 3 is the
one that used to fail invisibly.

The check ran from a temp directory and isn't in the repo — CLAUDE.md says no test
files unless I ask for them.

---

## Part 6 — Expired sessions, and why the bug was in two places

The browser had **no 401 handling anywhere**. My backend returns a clean
`401 "Token has expired."` and the frontend printed those exact words in red with
no way to recover. Refresh worked, but nothing on screen said so.

The reason it was missing is worth more than the fix. `useApi` and `useChatStream`
each had their own copy of get-token → fetch → throw. Same logic, written twice.
So the missing 401 branch was missing **twice**, and patching only the one I
noticed would have left the other broken.

One shared `fetchWithToken`, and the retry:

```ts
const fresh = await getToken({ skipCache: true });
```

Clerk hands out a cached token and only refreshes near expiry — so a 401 is
usually a *stale token in this tab*, not a dead session. A laptop reopened after
lunch hits this every time. Asking Clerk again with `skipCache` costs one round
trip and **fixes the common case with nothing appearing on screen at all.** Only a
second 401 raises `SessionExpiredError`.

No component changes were needed: they already display `err.message`, so they
picked up the better sentence for free.

I checked `skipCache` against the installed Clerk type definitions rather than
trusting that I remembered it right. It's real: *"Whether to skip the cache lookup
and force a call to the server instead, even within the TTL."* Verifying against
`node_modules` instead of memory is the habit — CLAUDE.md bans inventing library
APIs, and this is how you don't.

---

## Part 7 — BUILD.md's env vars are wrong

Day 10 needs LangSmith. BUILD.md says:

```
LANGCHAIN_API_KEY / LANGCHAIN_TRACING_V2 / LANGCHAIN_PROJECT
```

The current SDK reads `LANGSMITH_API_KEY`, `LANGSMITH_TRACING`,
`LANGSMITH_PROJECT`, `LANGSMITH_ENDPOINT`. The old names aren't documented as
aliases, so I'm not relying on them.

Same failure mode as everything else today: **wrong env var names don't error.**
Tracing just quietly does nothing and I'd be staring at an empty dashboard
wondering what I'd broken.

Caught before writing any code, by reading the current docs instead of trusting a
plan written weeks ago. Still needs correcting in BUILD.md.

---

## The through-line

Four bugs, one shape:

| Bug | What it looked like from outside |
|---|---|
| No LLM timeout | A slow answer |
| Raw errors to the user | An ugly but plausible error message |
| `errors="replace"` | A successful upload |
| No 401 handling | A confusing but honest-looking message |

Nothing crashed. Nothing logged an error. Every request returned a success status.
Day 9b's "give count" was the same thing wearing different clothes.

**The failures that hurt this project are not the ones that throw.** Which is
precisely the argument for Day 10: LangSmith exists so that "the app worked and
the output was wrong" becomes visible instead of invisible. I understand why that
day is in the plan a lot better than I did this morning.

---

## What this cost

Five files, no migration, no new dependency, no schema change. Four bugs fixed and
one BUILD.md note added. Not a planned day — it came out of asking one question
about caching and following what turned up.

## Still open

- **BUILD.md's env vars still say `LANGCHAIN_*`.** First thing on Day 10.
- Five bugs from the agent's report, agreed to fix after Day 10: the stuck
  "Loading…" state, a document stuck on `processing` forever, ingestion halting
  silently, `JSON.parse` throwing a parser string at the user, and no size ceiling
  on inlined images.
- One judgement call I haven't made: `chat.py:229` swallows a save failure, so I
  can read a complete cited answer that's gone on reload. It's documented as
  deliberate. Documented isn't the same as fine.
- Cross-user RLS on retrieval is *still* untested. Inherited from Day 7 and
  carried through 8, 9a, 9b and now this.
- Nothing committed at session end — five modified files across both apps.
