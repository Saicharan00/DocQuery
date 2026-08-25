# Day 12 — Captioning Fixed the Ceiling; Production Found the Bill

Personal learning log. Not read by Claude automatically — this is for me, to
recall what I built and why once the project is done. Not needed as context
for future sessions or development; it's a record, not a spec.

This wasn't on Day 12's original BUILD.md agenda (README, demo video, final
deploy). I paused that sequencing on purpose — the figure-retrieval ceiling
had been sitting in the README as a documented, unfixed limitation since
Day 10c, and I wanted a project that actually fixed its own known gaps, not
just wrote them down honestly. Correctness over speed, going in.

---

## Part 1 — The idea, and proving it before building on top of it

The root cause was never in doubt (found back on Day 10c): image chunks
embedded straight from JPEG pixels, no caption text, so a typed question
compared against a picture the way you'd compare a word against a color —
Day 7/11 measured that at 0.18–0.25 similarity, functionally noise.

The fix is almost embarrassingly simple in hindsight: caption each figure
with a vision model at ingestion, embed the *caption* instead of the pixels,
in the same 1536-dim space every text chunk already lives in. The chat model
still reads the real picture when answering — captioning only changes what's
*searchable*, never what's *shown*.

Before writing any pipeline code, I ran the idea in isolation: caption the
real Figure 1 from the Attention paper, embed the caption, embed the real
eval question, measure cosine similarity. **0.5371** — more than double the
old pixel-only band, on the first real call. That one measurement is what
turned "I think this will work" into "build the pipeline around it."

---

## Part 2 — Two bugs the smoke test and eval re-run caught, that a code review wouldn't have

**`HELPER_TIMEOUT` (10s) was tuned for the wrong kind of call.** `rag.py`
already had a short timeout for cheap text-only helper calls (titles, query
rewrites), and I reused it for `caption_image` without thinking hard about
why it was 10s and not 60s. First real smoke-test run: `litellm.Timeout`.
Obvious once it happened — an image upload takes longer than six words of
text — but not obvious from reading the code, only from running it. Fixed by
reusing `ANSWER_TIMEOUT` (60s), the same one `stream_answer` already uses for
calls that also carry images.

**The eval's own ground-truth data was stale, and nothing would have caught
it without re-running the eval.** While re-measuring `fig-01`, a direct
Supabase lookup by the id in `eval_qa.json` returned zero rows — the
Attention paper had been re-uploaded at some point since that id was
recorded, minting new chunk ids, and nobody had gone back to update the eval
file. Left as-is, `fig-01` would have scored a permanent miss forever,
regardless of how good retrieval got — not because retrieval was wrong, but
because the test itself was pointing at a chunk that no longer existed. Found
it by looking up the real chunk by document name instead of trusting the
cached id, which is now how the smoke-test script does the lookup
permanently rather than hardcoding an id that can silently rot.

---

## Part 3 — `eval.py report` is not a safe thing to run blindly

This one cost the most time relative to how avoidable it was. `scripts/eval.py
report` reads every cache file on disk and **fully rewrites**
`eval_results.md` from scratch. I knew that in the abstract — it's in the
script's own docstring — but I didn't connect it to the fact that three whole
sections of that file (cross-user isolation, the failure-mode analysis, the
entire Day 11.5 write-up) are **hand-written prose that report never
generates in the first place.** Running `report` after the eval re-run
replaced all three with nothing — not stale versions, just gone, because the
regeneration only knows about the sections it builds from cached data.

Caught it because the diff was suspiciously large (a `--stat` of +114/-198 on
a report that should have changed one row), not because I expected it.
Recovered the previous file with `git show HEAD:scripts/eval_results.md`,
manually merged the recovered sections back in alongside the real updated
numbers, and added the Day 12 write-up as a new section rather than losing
history to gain one. Saved as a durable memory this time, not just a lesson
learned in the moment: **never run `eval.py report` without diffing or
backing up the existing file first.**

---

## Part 4 — Backfill, and the pattern that made it boring

Existing image chunks — the ones uploaded before this fix existed — needed
the same treatment retroactively, or the fix would only ever apply to future
uploads. `scripts/backfill_captions.py` is a new three-phase script
(`fetch` → `caption` → `apply`), and it's a straight structural copy of
`eval.py`'s `retrieve`/`generate`/`judge` split, for the identical reason:
RLS is the only security boundary in this codebase, there is no
service-role key anywhere, and a Clerk session token lives about 60 seconds.
Anything that touches Supabase has to be a fast burst; anything that spends
real money can't be racing a dying token, so it has to run from a local
cache file instead.

Ran it against the real account: **7/7 image chunks backfilled**, total
spend **~$0.0018**. Found and fixed two of my own bugs in the script itself
mid-run — `fetch --limit 1`'s status line was computed *after* the limit
slice, so it printed "1 not yet captioned" when the true number was 6 (no
data was wrong, just the printed count); and a later "Captioned 7/6" message
that compared a lifetime-accumulated count against a single run's count.
Neither one touched correctness, both were confusing enough to be worth
fixing before trusting the next run's output at a glance.

---

## Part 5 — What production actually cost, that the smoke test didn't show

The whole thing merged and deployed, and the first real large upload after
that (a 290-page document, 75 extracted images — the existing cap) took
noticeably longer than before, though it did complete on its own.

The reason: captioning adds one Gemini call *per image*, on the same
account, same rate limit, that also answers every chat message. Nothing about
that was hidden — `documents.py` already had a rate-limit backoff loop for
exactly this shape of failure (a batch that wrote nothing means the provider
is throttling, wait it out) — but I hadn't actually priced out what 75 *new*
calls per large document does to that budget until it happened live. The
smoke test measured whether captioning *works*; it never measured what it
*costs* at the image count this app's own cap already allows. That's a real
gap in how I verified this before shipping it, not a bug in the shipped code
— the existing backoff handled it correctly, just slowly.

Left open, not fixed yet: whether to caption a few images concurrently
instead of strictly one-at-a-time, to finish faster inside the same rate
limit rather than accepting the wait. Flagged to the user, not decided.

---

## Part 6 — A question that looked like a bug and wasn't

Live-tested question: "how many pages does this book have?" Answer: not in
the sources. My first instinct was to wonder if this was the abstain-gate
problem again (Day 11.5 retired a version of this exact failure mode for
"what is this about"-style questions). It isn't the same thing. "What is
this about" was wrongly abstaining on content the document actually
contains, spread across passages retrieval should have found. "How many
pages" is *correctly* abstaining — page count isn't stated in any single
retrieved passage's text; the app technically knows it as file metadata
(it's what the upload progress bar counts against), but the chat pipeline
only ever shows the model retrieved *content*, never metadata. Telling these
two apart mattered: one was a real bug already fixed, the other is a correct
answer that just reads as a gap. Logged as a "what's next" README item
instead of a fix, since answering it would need a genuinely separate,
non-retrieval code path.

---

## What Day 12 cost

Two commits on `worktree-day12-image-captioning`, merged to `main` via PR
#53 (plus one small follow-up commit after merge, for the README addition
above — its own PR still open). Three files touched in `apps/api`
(`rag.py`, `documents.py`, `ingestion.py`'s docstring), two new throwaway
scripts (`smoke_test_caption.py`, `backfill_captions.py`), `eval.py`'s stale
comment fixed, `eval_qa.json`'s one-line data fix, `eval_results.md`/
`README.md`/`BUILD.md` all updated with real numbers instead of "known
limitation" framing. No schema migration — reusing `chunks.content` instead
of adding a `caption` column turned out to be both the lazier choice and the
better one, since it fixed the frontend's citation preview and image alt
text for free in the same change.

Real money: the smoke test (one vision call, two embed calls), the backfill
(~$0.0018 for 7 images), and the targeted eval re-run (~$0.0023 for
generation, plus judging) — all confirmed before spending, none of it
needed separate approval beyond what was already flagged going in.

## Still open

- **Concurrent captioning during ingestion.** Sequential, one image at a
  time, is what hit the rate-limit wall on a 75-image document. Not fixed,
  just diagnosed and flagged.
- **The abstain-gate saga's branch got its own PR (#52) merged mid-session,
  before this session even started** — worth remembering next time I'm
  reasoning about what's merged vs. pending from a stale memory snapshot
  instead of checking `git log` directly. I gave the user a redundant PR
  link for it before catching this.
- **Document-metadata questions** (page count, upload date) are a named,
  scoped-out gap now, not a silent one — see README's "what's next".
- **`eval.py report`'s destructive-regeneration behavior** is still true of
  the script itself, not just a one-time mistake. The next person to run it
  (including future me) needs the same warning this log is recording.
