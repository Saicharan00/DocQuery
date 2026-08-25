# Bugs fixed — 2026-08-25

Four bugs, found and fixed live in one session, each starting from a real
symptom hit while actually using the app rather than from a code audit. All
four verified working in production afterward.

---

## 1. Uploads with images took 6-10 minutes — VERIFIED

`apps/api/app/routers/documents.py`, `apps/api/app/services/ingestion.py`

**Symptom:** a document with several figures, previously uploading in under
10 seconds, now took minutes and looked like it was "breaking and retrying"
over and over.

**Root cause, in two parts.** Day 12 added a real vision-model call
(`rag.caption_image`) per image before it's embedded, run strictly one at a
time — so a document with N images paid the *sum* of every call's wait, not
the slowest one. Worse: images were grouped 8 at a time, and every image in
a group had to caption successfully before *any* of them were written to
the database — so one slow or timed-out image threw away up to 7 already-
successful captions, which then had to be redone (and re-billed) on the
very next retry.

**Fix, two commits.** `d435528` runs captions through a small thread pool
(5 at once) instead of one at a time — several waits overlap instead of
stacking, at the same cost, since concurrency doesn't add extra calls, just
removes the idle time between them. `928ce79` shrinks the group size
(`IMAGE_EMBED_BATCH_SIZE`) from 8 to 3, so a timeout now wastes at most 2
finished captions instead of up to 7. The concurrency cap was deliberately
left higher than the group size — it costs nothing sitting unused today,
and stays a real safety ceiling if the group size is ever raised again.

---

## 2. Deleting a document mid-upload silently did nothing — VERIFIED

`apps/web/src/components/document-list.tsx`,
`apps/web/src/app/dashboard/page.tsx`

**Symptom:** clicking delete on a document that was still ingesting had no
visible effect — no confirmation, no error the user would notice, and the
document stayed.

**Root cause.** The backend deliberately refuses a delete while a step is
actively running (409) — deleting mid-write could otherwise orphan files in
storage. The code comment even promised "the wait is seconds, not minutes,"
but the frontend never actually implemented that wait: it called delete
once, got the 409, showed a small inline error, and stopped. There was a
sharper problem underneath, too: the browser's own upload loop re-claims
the next step within milliseconds of the previous one finishing, so even a
naive retry would almost always lose that race and could effectively never
succeed on a document still being actively driven.

**Fix (`232ef36`).** Delete now stops that document's own ingest loop first
(aborting its `AbortController`, so it stops claiming new steps), *then*
retries the delete every 2 seconds for up to a minute — long enough to
outlast whichever single step was already in flight the moment delete was
clicked. Added a `window.confirm()` prompt too (matching the existing
pattern already used for deleting a conversation), since deleting a
document should always ask first.

---

## 3. Chat answers cut off with "the connection stalled" — VERIFIED

`apps/web/src/lib/api.ts`

**Symptom:** a chat answer would stream in almost completely, then fail
with "The connection stalled before the answer finished" — on the second
and third questions of a conversation, not the first.

**Root cause.** Two independently-tuned timeouts were supposed to agree,
and didn't. The backend allows a single model call up to 60 seconds
(`ANSWER_TIMEOUT` in `rag.py`) before giving up and sending its own,
specific error. The frontend's stall-watchdog (`IDLE_MS`) was set to just
20 seconds — *tighter* than the backend's ceiling, not looser, despite its
own comment claiming it was "deliberately generous." Any model call taking
between 20 and 60 seconds got killed by the browser before the backend's
timeout — and its more useful error message — ever had a chance to fire.

**Confirmed, not guessed:** a real LangSmith trace showed two Gemini calls
in the same session taking 59s and 71s and erroring server-side — matching
`ANSWER_TIMEOUT` almost exactly, and well past the frontend's 20s. Testing
a different model (GPT-5.4-nano) on the identical code path had no issue,
which pinned this down as Gemini being genuinely slow that day — plausibly
from the same account's heavy captioning/eval testing earlier — rather than
a bug in the streaming code itself.

**Fix (`984f00a`).** Raised `IDLE_MS` to 65 seconds, just above
`ANSWER_TIMEOUT`, so the backend's own timeout always wins the race. A
recoverable pause now gets a chance to actually finish instead of being
killed early, and a genuine timeout now surfaces the backend's specific
message instead of the browser's generic one.

---

## What two of these three have in common

The batch-size/concurrency pair (bug 1) and the two timeouts (bug 3) are
the same underlying shape of bug: **two numbers that were supposed to stay
in sync, and quietly didn't**, because one was tuned without revisiting the
other. Worth deliberately checking for this pattern whenever one half of
such a pair changes again — a batch size next to a concurrency cap, a
frontend timeout next to a backend one, and so on.

---

Also shipped the same session, cosmetic rather than a bug fix: a small
notice added under the upload box ("documents with charts or figures take a
little longer") so a slow image-heavy upload reads as expected behavior
rather than something broken.
