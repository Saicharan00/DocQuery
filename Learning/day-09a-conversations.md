# Day 9a — Building the Door Back In

Personal learning log. Not read by Claude automatically — this is for me, to
recall what I built and why once the project is done. Not needed as context for
future sessions or development; it's a record, not a spec.

Written after the fact, from the session transcripts, so it's a reconstruction
rather than same-day notes — but the failures and corrections in it are the real
ones.

---

## The problem in one sentence

Every conversation I'd ever had with this app was **already saved in Postgres**
and there was no way to read one back.

Not "not saved". Saved. `_save_exchange` had been writing both rows of every
exchange since Day 7. The data was sitting there, correct, with RLS on it — and
the frontend had no way to ask for it, because a browser can't talk to a database
and there was no endpoint that said *"give me a conversation's messages."*

The data existed. **The door didn't.** Day 9a is the door.

---

## Part 0 — Two kinds of amnesia

Day 9 was described to me as curing two, and they turned out to be different
enough that I split the day in half.

| | What it means | Fixed in |
|---|---|---|
| **Between visits** | Close the tab and the conversation is gone *to you* — it's in the database with no way in | **9a** |
| **Inside one conversation** | Every message is treated as the first thing you ever said. "What about the second one?" gets embedded literally | **9b** |

The split was mine and I'm glad of it. 9a is plumbing — endpoints, routes, a
sidebar. 9b is a retrieval problem with a real risk of making things worse. They
share a day number and almost nothing else.

---

## Part 1 — The two words I kept nodding at

I stopped the session early on and said: *whenever you say **endpoints** I don't
understand.* Then, a minute later: *what is this **router**?*

Both explanations were worth the interruption, so they go in the log.

### An endpoint is one specific thing the backend can be asked to do

The backend is a program sitting at a web address doing nothing, waiting. An
endpoint is one named request it knows how to answer, and it takes two things to
make one:

1. **a verb** — GET, POST, PATCH, DELETE
2. **a path** — `/documents`, `/chat`

`GET /documents` is one endpoint. `POST /documents/upload` is a *different* one.
Same server, different job. Send the server anything not on its list and it says
404 — *I don't know that one.*

The verbs are convention, not enforced, but everything in the ecosystem assumes
them:

| Verb | Means | Practical consequence |
|---|---|---|
| GET | read, change nothing | safe to retry |
| POST | create / do work | retrying might upload twice |
| PATCH | change part of an existing thing | **Day 9a adds the app's first one** |
| DELETE | remove | |

And `{curly braces}` in a path are a blank to fill in. `DELETE /documents/{id}`
matches `DELETE /documents/9f3a-1c22…`; FastAPI pulls the id out of the URL and
hands it to my function as a variable. One definition, every document.

### A router is a folder for endpoints

Everything hangs off one object, `app`, in `main.py`. I *could* define every
endpoint directly on it — and `main.py` would be two thousand lines. A router is
a small stand-in for `app` that lives in its own file:

```python
router = APIRouter(tags=["me"])   # 1. an empty box, connected to nothing

@router.get("/me")                # 2. hang an endpoint on the box
                                  #    (@router, not @app — this matters)

app.include_router(me.router)     # 3. in main.py — NOW it's reachable
```

Delete line 3 and `me.py` still exists, still has valid code, still defines
`/me` — and `GET /me` returns 404. That's the mistake worth making once.

Two things I'd have got wrong on my own:

- **The browser has no idea routers exist.** There's no router on the wire. It's
  purely how I file my own code. Same behaviour, different filing cabinet.
- **`prefix="/documents"` is the one thing that saves real typing.** It sticks
  itself in front of every path in the box, which is why `chat.py` has the
  odd-looking `@router.post("")` — empty path plus `prefix="/chat"` equals
  exactly `/chat`.

---

## Part 2 — Day 1 had already done half the work

Before writing anything, the migrations got read. Three things were already
true, from Day 1 and Day 2:

- `001_init.sql` had created `conversations` and `messages` **with RLS**
- `002_grants.sql` had already granted `update` and `delete` to signed-in users
- `messages.conversation_id` was declared `references conversations (id) **on
  delete cascade**`

**So Day 9a needed no migration at all.** That last line is the good one:
deleting a conversation removes its messages *inside Postgres*, with no code
from me.

Worth noticing as a pattern — the boring, careful schema work on Day 1 quietly
paid out eight days later. I wouldn't have felt that at the time.

---

## Part 3 — The hinge, and a silent failure I'd never have caught

`PATCH /conversations/{id}` — rename — was flagged as the risky piece, and the
reason is specific and nasty:

> **The first write to an *existing* row anywhere in this app.** Everything
> before it was an insert or a delete.

And this stack has a documented silent-failure mode: **when RLS rejects a write,
PostgREST returns an empty result set, not an error.**

Read that again, because it's the lesson of the day. A rename that RLS refuses
comes back as `200 OK` with nothing in it. The endpoint looks like it worked.
The UI would show the new title (from local state), and the database would be
unchanged. **The status code proves nothing.**

The only proof is reading the title back out afterwards. So the check script did
exactly that — rename to `RENAME TEST`, list again, compare — before a single
line of sidebar was written.

```
title is now : RENAME TEST     ← PASS
```

Two things fell out of that run for free:

- **`updated_at` didn't move.** Still the same value from before the rename, on
  purpose: `updated_at` means "when was this last talked in" and it's what the
  sidebar sorts by. Renaming an old conversation must not shove it to the top as
  though it had new messages.
- **An open question got answered.** The plan had admitted uncertainty about
  whether supabase-py returns changed rows from `.update()`. It does — the
  response body was the row. The look-up-first design stayed anyway, because
  that's what distinguishes *"not yours / doesn't exist"* (404) from *"the write
  was refused"* (502).

I'd rather be told "I don't know, the test will tell us" than be given a
confident guess. That happened twice on this day and both times the test settled
it in seconds.

---

## Part 4 — What actually proves a cascade

Delete was left for last because it's the one call that can't be undone. The
first version of the check script claimed something it couldn't show, and this
got corrected mid-flight:

> Step 4 — messages returning 404 — does **not** prove the messages were
> deleted. It 404s because the *conversation* is gone, and every route to
> messages goes through that check first.

**The real proof is the `204` itself.** `conversation_id` is a foreign key, and
Postgres refuses by default to delete a parent row that still has children — it
raises a foreign-key violation, which my handler turns into a 502. So a
successful delete of a conversation that had messages *means* those messages went
with it. A silent orphan isn't one of the possible outcomes.

The run:

```
title    : hey hi
messages : 8          ← eight, not the two we guessed
HTTP 204              ← the cascade proof
HTTP 404              ← its messages endpoint
HTTP 404              ← deleting it a second time
```

That last line is a small bonus worth keeping: deleting an already-deleted
conversation returns 404, not another cheerful 204. That's the ownership check
genuinely gating the write rather than the endpoint blessing any UUID I throw at
it.

**Generalising:** "did the API return success?" and "did the thing happen?" are
different questions, and on this stack they come apart in both directions. I now
expect to need a *read-back* for every write.

---

## Part 5 — `app.routes` doesn't list your endpoints

Small, sharp, cost ten minutes.

To check the new router was wired in, the obvious move is to import the app and
loop over `app.routes`. It printed **four** routes — all FastAPI's own docs
pages, none of mine. Looked like the router hadn't been plugged in.

It had. Printing the count showed **9**: four docs pages plus **five
`_IncludedRouter` objects**, one per router. This FastAPI version keeps an
included router as a single wrapper object instead of flattening it into
individual routes, so the loop was reading the wrong level entirely.

The fix is to ask the app to describe itself, which lists real paths regardless
of internals:

```powershell
.\.venv\Scripts\python.exe -c "from app.main import app; [print(', '.join(sorted(m.upper() for m in ops)).ljust(14), p) for p, ops in sorted(app.openapi()['paths'].items())]"
```

A two-second check that a router is actually reachable, before spending a deploy
finding out. It's in my notes now.

---

## Part 6 — Auto-titling, and why the sidebar demanded it

The very first `GET /conversations` returned 12 rows, and four of them were
titled:

```
tell me about this project
tell me about this project
tell me about this project
tell me about this project
```

That's `_resolve_conversation` doing `message[:60]` — the placeholder title.
Perfectly reasonable, and it makes a sidebar you can't use. **A sidebar you
can't tell apart is a sidebar nobody clicks**, which would have made the rest of
the day pointless.

So: one cheap model call after the first answer, turning the question into a
short title.

| Decision | Why |
|---|---|
| Always the cheap default model, never the one the user picked | A title is a label in a sidebar. No reason to bill the expensive model, and no reason for the same question to get a different label depending on who answered. |
| Runs **after** `_save_exchange`, inside the stream | The answer is fully on screen by then, so ~400ms is invisible. Titling first would delay the first token of every new conversation for a cosmetic label. |
| Failure is logged and swallowed | The truncated placeholder is still there. Breaking a finished answer over a label would be absurd. |
| `_resolve_conversation` returns `(id, is_new)` | It already knows whether it inserted a row and was throwing that fact away — without it, every message in a long chat pays to re-title. |

Cost: about **$0.00003** per new conversation. A thousand of them is three cents.
I was told the number anyway, which is the right call — CLAUDE.md says money is
my decision however small it is.

### The one thing I overruled

There was a proposal to **drop** the `title` SSE event, on the reasoning that the
sidebar refetches when the URL changes and would pick the title up anyway. I said
keep it.

It was the right instinct for a reason I only half-had at the time: relying on
the refetch means relying on an ordering guarantee between two independent things
(the title write finishing, and the sidebar's fetch firing). The event makes it
explicit. And the verification showed the ordering holds regardless:

```
event order : conversation -> sources -> title -> done   (+ 11 token frames)
payload     : {"title": "Positioning Accuracy Factors"}
in database : Positioning Accuracy Factors
PASS
```

`Positioning Accuracy Factors` instead of `can you tell me what this document
says about the things tha`. And `title` lands **before** `done`, which is what
guarantees the sidebar's refetch can't race it.

(11 token frames for a whole answer just means Gemini sends words in batches
rather than one at a time. Normal.)

---

## Part 7 — Frontend: one URL per conversation

The chat UI was one page holding all its state in React. A conversation existed
in memory and nowhere else. The sidebar needed somewhere to *send* me when I
clicked a row, and "somewhere" has to be a URL, or reload and the back button
both break.

But two entry points must not mean two copies of a 300-line component. So:

| File | What |
|---|---|
| `components/chat-view.tsx` | **new** — the whole UI, moved, plus a `conversationId` prop |
| `app/dashboard/chat/page.tsx` | shrinks to `<ChatView conversationId={null} />` — the "new chat" |
| `app/dashboard/chat/[id]/page.tsx` | **new**, ~4 lines. `[id]` is a *dynamic segment*: square brackets mean "match anything here and give me the value". One folder serves every conversation I'll ever have. |

Three things I'd have got wrong from memory alone:

**1. `params` is a Promise now.** `apps/web/AGENTS.md` warns that this is Next
16.2.12 and not the Next anyone remembers, so the local docs in `node_modules`
got read instead of trusting recall. `params` must be awaited. Worth knowing that
the docs ship *inside the installed package* — the version I'm actually running,
not whatever's current on the website.

**2. `window.history.replaceState`, not `router.replace`.** After the first
answer, the URL has to change from `/dashboard/chat` to `/dashboard/chat/{id}`.
`router.replace` is a **real navigation** — it would unmount the chat view and
refetch the answer already sitting on screen. The native History API is
documented to integrate with the Next router, so `usePathname` still sees the
change and the sidebar still refetches. Same payoff, no remount.

**3. `[id]/page.tsx` keys `ChatView` by id.** Without the key, clicking from one
saved conversation to another reuses the same component instance — and the next
question gets filed under the conversation I just left.

The check for all of this was `npm run build`, which type-checks every file the
way Vercel will:

```
✓ Compiled successfully in 5.9s
├ ƒ /dashboard/chat
├ ƒ /dashboard/chat/[id]        ← new
```

The route table is the proof the dynamic segment registered — a mistyped folder
name doesn't error, it's just **absent from the list**.

---

## Part 8 — The bug I found, and how it got root-caused

I tested the sidebar and reported: **rename doesn't work.**

The diagnosis didn't start in the React code. It started in the API's log:

```
DELETE ... 204
DELETE ... 204
DELETE ... 204
```

**No `PATCH` ever reached the server.** Not a failed one — none at all. That's
enormously narrowing: the request was never being sent, so the bug was in the
browser before the fetch. And since my deletes went through, clicks registered
and the buttons weren't unreachable. The only path left was the line before the
fetch.

The console said it outright:

> `prompt() is not supported.`

**`window.prompt()` is not universally available.** The browser refuses the call
and throws before any request can be made. `confirm()` in the same component
worked fine — that's why delete worked and rename didn't.

The fix isn't to retry it, it's to stop using it: the title becomes an inline
`<input>` in the row. Enter saves, Escape cancels. No dialog for a browser to
block, and better UI than a 2005 popup regardless.

**The lesson is the order of investigation.** "Rename doesn't work" is a symptom
in the UI, and the instinct is to read the UI code. Checking whether the request
*arrived* took one command and eliminated the entire backend plus most of the
frontend in one move.

---

## Part 9 — Two testing tricks worth keeping

**Checking CORS without reading `.env`.** The allowed origins live in an env var
I'm not allowed to read (CLAUDE.md, hard rule). So instead of reading the file,
ask the API itself — send the preflight request a browser would send:

```powershell
curl.exe -s -i -X OPTIONS http://127.0.0.1:8000/conversations `
  -H "Origin: http://localhost:3000" -H "Access-Control-Request-Method: GET"
```

`access-control-allow-origin: http://localhost:3000` came back. Question
answered, secret untouched. **When you can't read the config, interrogate the
behaviour.**

**Pointing the local site at the local API without editing any file:**

```powershell
$env:NEXT_PUBLIC_API_URL="http://127.0.0.1:8000"; npm run dev
```

A real environment variable overrides `.env.local` in Next, so both halves ran
locally against the real Supabase with nothing on disk changed and nothing to
remember to undo.

---

## Part 10 — The carousel

Somewhere in the middle of the day I asked for the sidebar to be *"an interactive
3D card stack carousel with fan-out animation, drag gestures and auto advance."*

I got pushed back on, and the pushback was right:

- **Auto-advance on a navigation list moves the thing you're aiming at.** You go
  to click a conversation, it rotates, you open the wrong one.
- **A stack shows one card where a list shows twelve.** The entire reason the
  sidebar exists is *scanning* — which of these was the one about the ionosphere?
- **Drag-to-select is slower than click-to-select**, and keyboard/screen-reader
  support has to be rebuilt by hand, where `<Link>`s in a `<ul>` get it free.

What I want to remember is that the answer wasn't "no". It was **"wrong
surface"** — the suggestion was that a card stack genuinely fits the *citations
panel*, where there are 5 discrete sources that really are browsed one at a time
rather than scanned, and which is currently a cramped `<details>` list.

Wanting the app to look like more than a Tailwind default is a fair goal for a
portfolio. The useful question is which surface can afford it.

---

## Part 11 — `--reload` runs two processes

At the end of the day, shutting the servers down took four attempts, and the
sequence is a nice miniature of debugging honestly.

1. Kill whatever's listening on ports 8000 and 3000. Port 3000 freed. 8000 didn't.
2. Kill again. Still there.
3. `Get-Process` on the PID → **empty**. Conclusion: the process is gone, the
   port entry is stale, Windows will clear it.
4. **That conclusion was wrong**, and the check that disproved it was one line:
   the port genuinely still answered `HTTP 200`. Something was alive.
5. Found by name instead: a second python process — the uvicorn **worker child**.
   `--reload` runs a parent reloader *and* a child server, and killing the parent
   left the child holding the socket.

The wrong conclusion in step 3 lasted about a minute because step 4 tested the
thing that actually mattered — *does anything still answer?* — rather than the
proxy — *does the process table list it?*

I'd also asked for `--reload` earlier in the day, which is why there were two
processes at all. It watches the files and restarts on save, which saved several
manual restarts. Fair trade, but it changes how you kill it.

---

## Part 12 — Three commits, not one

Nine changed files went in as three commits rather than one, on the principle
that **each commit should be one idea** — so `git log` reads as a story and any
single change can be reverted alone:

1. `feat(api): conversations router — list, read, rename, delete`
2. `feat(api): auto-title a conversation after its first answer`
3. `feat(web): conversation sidebar and a URL per conversation`
4. `docs: tick Day 9a boxes and record the 9a/9b split`

(Four, counting the docs one. The first had been committed at the end of the
earlier session so the work survived a session boundary.)

---

## Deviations from BUILD.md, both deliberate

- **The sidebar lives in `dashboard/chat/layout.tsx`, not `dashboard/layout.tsx`.**
  BUILD.md said the whole dashboard. Scoping it to chat keeps the documents page
  a full-width upload screen, which is what it wants to be.
- **`chat/layout.tsx` was held back from step 4 to step 5.** Until the sidebar
  existed, that file would have been an empty wrapper rendering `{children}` —
  scaffolding for later, and later can scaffold for itself.

---

## What Day 9a cost

Two sessions, four commits, one real bug found by me in the browser. Under a cent
in API calls, because every backend endpoint was proven against the real Supabase
from a **local** API before any UI was built — no Railway deploy, no merging
unfinished work to `main`.

That local-testing recipe is the most reusable thing to come out of the day: the
API runs on my machine, verifies a token copied from the *live* site against
Clerk's public keys over the internet, and reads the *same* Supabase. Same auth,
same data, **same RLS**. Only the address differs — so anything RLS-related
proves exactly what a deploy would prove.

## Still open at the end of the day

- **Day 9b.** I asked a follow-up — *"give count"* after an answer listing seven
  names — and got *"the provided sources do not contain enough information."*
  Not a 9a bug: retrieval embeds the literal words, and nothing in my documents
  is near "give count". The model refusing rather than inventing a number is the
  grounding rule working. That's the whole target for 9b, now reproduced in
  BUILD.md instead of described in the abstract.
- Cross-user RLS on retrieval, still untested — inherited from Day 7.
- The PR needed opening by hand: `gh` isn't installed on this machine.
