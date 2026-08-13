# Day 9b — Teaching the Search What Was Already Said

Personal learning log. Not read by Claude automatically — this is for me, to
recall what I built and why once the project is done. Not needed as context for
future sessions or development; it's a record, not a spec.

Day 9a made conversations *persist*. It did not make them *continuous*. Every
question this app had ever answered, it answered alone — `/chat` took one
message, embedded it, searched, and prompted the model, and nothing about the
previous turns reached either the search or the model.

I found that out by hand. After an answer listing the factors that affect
positioning accuracy, I typed **"give count"**. The model refused to answer.

Day 9b is the day those two words started working.

---

## Part 0 — The bug, and why the refusal was the healthy part

My first instinct was that the model had failed. It hadn't. `SYSTEM_PROMPT`
says: ground every claim in the sources, and if the sources don't contain the
answer, say so plainly and stop. The sources genuinely didn't contain it. The
model did exactly the right thing with what it was handed.

So the failure was upstream, and this is the bit worth remembering:

> **"give count" was embedded literally.**

Embedding means turning text into a list of numbers that stands for its meaning,
and retrieval means finding the chunks whose numbers are closest. Those two words
carry no subject at all. They have no meaning to stand for. Whatever the numbers
for "give count" are, they sit nowhere near a passage about ionospheric delay —
so retrieval returned effectively random chunks, and the model was handed noise
and asked about counting.

**The status code was 200 the whole time.** Nothing errored. This is a class of
bug I hadn't met before: everything succeeds and the output is still worthless.

---

## Part 1 — One symptom, two defects

The thing that made this day take planning rather than an afternoon is that
"give count" fails for **two independent reasons**, and fixing either one alone
gets you nothing.

| Defect | Symptom | Fix |
|---|---|---|
| The **model** doesn't know what was said before | It can't resolve "the second one" even if the right chunks arrive | Send the last 3 turns of text |
| The **search** doesn't know what was said before | The right chunks never arrive at all | Rewrite the question before embedding |

Fix only the model's memory and you get a model that understands the question
perfectly and has the wrong sources in front of it. Fix only the search and you
get correct sources and a model that still can't parse "what about it?".

Both, or neither is worth doing. That was the plan's first real decision.

---

## Part 2 — The ordering that is the entire day

```
"give count"
     |
     v
load the last 3 turns          <- the subject exists here and nowhere else
     |
     +--------------------+
     |                    |
     v                    v
rewrite the question   history into the prompt
     |                    |
     v                    |
EMBED the rewrite         |     <- after this point the vector exists
     |                    |        and no prompt can un-choose it
     v                    |
retrieve the right chunks |
     |                    |
     +--------------------+
                v
   answer the ORIGINAL question
```

Read it top to bottom and nothing in it is optional. The one arrow that matters
most is the one into **EMBED**: rewriting has to happen *before* the question
becomes a vector. Afterwards is too late — not "less effective", but literally
pointless, because the search has already been performed against the wrong
numbers and no amount of clever prompting downstream can undo that.

I've written it out because in the plan this was a diagram, and the diagram is
what made me stop trying to solve it with a better system prompt.

---

## Part 3 — The rewrite is for the search, and only for the search

This was the point I asked about explicitly, and I'm glad I did, because the
answer turned out to be the most interesting design decision of the day.

When the rewriter turns "give count" into *"How many factors affect GPS
positioning accuracy according to the document?"* — **that rewritten question is
never sent to the model that writes the answer.**

It is:
- embedded, to produce the vector,
- written to the log, so Day 11 can audit what the retriever actually searched,
- and then thrown away.

Never saved. Never shown to me. Never in the prompt.

The reason is that I typed "give count", and answering a question I didn't ask —
even a better-phrased version of it — is a small lie the system would tell every
single time. If the rewriter drifts slightly, I'd never know: I'd see an answer
to a question I never saw. So the model gets my original two words, plus the
conversation history that makes those two words legible.

**Two different consumers, two different needs, from one piece of data.** The
search needs a self-contained sentence because a vector has no context. The model
needs the actual words I typed, because that's what I asked. The history serves
both, in different shapes.

---

## Part 4 — The bug that had been there since Day 1

This is the part I'd never have found by looking at the feature.

`messages.created_at` is declared `default now()`. To load history I wanted the
last few messages, so: `order by created_at`. Obvious.

Except **Postgres' `now()` is the transaction timestamp, not the clock.** It
returns the moment the transaction started, and it returns that same value to
everything inside that transaction, however long it runs.

And `_save_exchange` writes the question and the answer in **one insert**. One
statement, one transaction, one `now()`. So the two rows of every exchange I have
ever saved carry **byte-identical timestamps.**

Which means `order by created_at` has a genuine tie on every single exchange, and
nothing in SQL guarantees how a tie resolves. Postgres is free to hand back the
answer before the question. That would feed the model a conversation reading
*assistant, user, assistant, user* — garbled, subtly wrong answers, and no error
anywhere.

It had been latent since Day 1. Day 9a's "load a saved conversation" screen used
the same ordering and worked purely by luck.

### The fix, and why it looks like a typo

Add `role` as a second sort key. Both places. But the two lines look
contradictory:

```python
# load_history — newest first, so limit grabs the recent end
.order("created_at", desc=True).order("role")

# list_messages — oldest first, because a conversation is read from the top
.order("created_at").order("role", desc=True)
```

They differ because the *time* directions differ. Alphabetically `assistant`
comes before `user`. In `list_messages` time runs forwards, so I need `user`
first on a tie → `role` descending. In `load_history` time runs backwards (a trick
so `limit` takes the recent end rather than the beginning) and the whole list gets
reversed afterwards — so the tie-break has to be *pre*-reversed too → `role`
ascending.

Both lines are commented in the files, because in six months this is exactly the
kind of thing I'd "fix" into a real bug.

**The general lesson:** a `default` in a schema is a behaviour, not a decoration.
`now()` looked like "the time this row was written" and it isn't quite.

---

## Part 5 — The hinge: quality is not a status code

Every other piece of this day either works or throws. `load_history` returns rows
or raises. `build_messages` builds a list. The wiring is plumbing.

`rewrite_query` is different, and the plan called it the hinge for this reason:

> It can run perfectly, return HTTP 200, hand back a real non-empty string, and
> still not fix the bug.

Because "is this a good rewrite?" is a judgement about **quality**, and there is
no status code for quality. If I'd wired it into `chat.py` first and tested
end-to-end, a bad rewrite and a good rewrite would have looked identical from the
outside — both produce an answer.

So before anything depended on it, we called the function on its own from a
throwaway script, with my real failing exchange hardcoded as history, and
**printed the strings**.

That is the whole technique and it cost about five minutes.

### What reading the strings caught

| in | first attempt | verdict |
|---|---|---|
| `give count` | `How many positioning accuracy factors are listed?` | ✅ the bug, fixed |
| `What is multipath error?` | `What is multipath error **in the context of positioning accuracy**?` | ⚠️ added words I didn't ask for |
| `What does the document say about ionospheric delay?` | `…about ionospheric delay **affecting positioning accuracy**?` | ⚠️ same habit |

The first row was a success and I'd have shipped on it. Rows 2 and 3 are the
reason not to. The instruction said *"if the question already stands on its own,
reply with it unchanged"* and the model was ignoring that — quietly stapling the
conversation's topic onto questions that were already fine.

Inside a single-subject chat that's harmless. The failure is the **topic pivot**:
ask about positioning accuracy, then ask *"what does the invoice say about the
total amount due?"*, and a rewriter with that habit drags "positioning accuracy"
along and searches the wrong document entirely.

Two extra rules fixed it — *don't add the topic of the conversation to a question
that didn't mention it*, and *the user is allowed to change the subject* — and I
re-ran with the pivot case added. It came back untouched. That test now exists
because reading three strings suggested a fourth one worth trying.

---

## Part 6 — What I chose not to fix

Same check, one more case:

```
in  : 'explain the third one'
out : 'Explain ionospheric delay in the context of the factors affecting positioning accuracy.'
```

It resolved the pronoun — genuinely impressive, given "the third one" contains no
noun at all. And it **counted wrong**. The list was satellite geometry,
ionospheric delay, tropospheric delay… so the third is *tropospheric*. It picked
the second.

Cheap models are bad at ordinal positions in lists and no prompt rule fixes that
reliably. My options were: upgrade the rewriter to an expensive model on every
follow-up, build something complicated, or ship it and measure.

I shipped it, with the ceiling written into the code as a `ponytail:` comment
naming the upgrade path. It's survivable for a specific reason: retrieval is
*fuzzy*. Six factors listed together almost certainly live in neighbouring
chunks, so a search for the second one still pulls the third into the top five —
and the answering model gets my original "explain the third one" plus the history,
so it can pick correctly from what arrives.

**But that's a plausible argument, not a measurement.** Day 11's eval harness is
what decides whether it's true. Writing the limitation down beats pretending it
isn't there.

---

## Part 7 — The optimisation I proposed and had to retract

Early on the idea came up of only rewriting when the question *looks* like a
follow-up — short, or contains a pronoun — to save a model call on questions that
don't need one.

It's wrong, and the way it's wrong is instructive. **"give count" has no pronoun
and is short in a way no length rule catches on purpose.** The heuristic designed
to save latency would have skipped the exact case the entire day exists to fix.

So: the rewrite fires whenever history is non-empty, full stop. The first message
of a new conversation has no history, so it costs nothing — which is where the
saving actually was all along. The server log proves it: a fresh conversation
shows two model calls (answer + title) and no `Rewrote` line at all.

---

## Part 8 — Degrade, don't fail

Both new steps are wrapped so that failure falls back to yesterday's behaviour:

- history load fails → answer with no memory, log loudly
- rewrite fails → search the original question, log loudly

The reasoning is the same bargain `_retitle` already makes. By the time these run,
the user is waiting on an answer they're going to be billed for. Killing that
because a cheap helper hiccuped is a bad trade — especially when the fallback is
"the app as it worked yesterday", which is not an error state, it's a feature
level.

`except: pass` is banned in this project and this isn't that: every fallback
writes a full traceback to the log. Silent is the thing to avoid, not
*recoverable*.

---

## Part 9 — Proof

The log line that closed the day:

```
INFO app.routers.chat: Rewrote 'give count' as
  'How many factors affect GPS positioning accuracy according to the document?'
```

What it retrieved:

```
[1] major project-LAST2.3nishanth updated.pdf p77  sim=0.591
[2] ...p75  sim=0.515
[3] ...p78  sim=0.491
[4] ...p74  sim=0.439
[5] ...p76  sim=0.390
```

Pages 74–78. **Consecutive.** The search landed on one contiguous section of the
right document instead of scattering — that's what a good vector looks like from
the outside. (Cohere's similarity scores are compressed; 0.59 is strong here, not
weak. A near-verbatim quote measured 0.68 back on Day 7.)

And the answer came back grounded, cited, and counted six.

The other half of the proof matters just as much: on the **first** message of a
new conversation, the log shows two model calls and no rewrite line. Day 9a's
behaviour, unchanged, no latency added. A new feature that quietly taxes the path
that didn't need it is a regression wearing a nice hat.

---

## Part 10 — A testing gotcha that cost real money

My test script used `Invoke-WebRequest`, which in PowerShell 5.1 hands the
response to Internet Explorer's HTML engine. IE has never been set up on this
machine, so it tried to prompt and died:

> Windows PowerShell is in NonInteractive mode. Read and Prompt functionality is
> not available.

One flag fixes it: `-UseBasicParsing`.

The part worth remembering is what I got wrong about it. I said "no API call was
billed" — but `Invoke-WebRequest` **sends the request, receives the response, and
then** tries to parse it. The answer had already been generated and paid for. The
failure was on the last step, after the money was gone.

*A client-side error does not mean the server didn't do the work.*

---

## What Day 9b cost

Four files, one commit. No migration, no new dependency, **no frontend change at
all** — the browser was already sending `conversation_id`, and history lives in
Postgres, so the server can read it without being told anything new. The best
kind of feature: the client didn't need to know it happened.

## Still open

- **Day 11 measures whether any of this helped.** That's not a formality — the
  ordinal miss in Part 6 is a real open question, and "the chunks looked right to
  me" is not a measurement.
- Cross-user RLS on retrieval is *still* untested. Inherited from Day 7, carried
  through Days 8, 9a and now 9b.
- The `day9a-conversations` PR is still unmerged, and now holds work whose name
  it doesn't describe. Renaming the branch would break the PR link, so it stays
  wrong on purpose and the commit message says so.
- No Day 9a learning file exists. This one is 9b only.
