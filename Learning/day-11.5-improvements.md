# Day 11.5 — Two Premises That Didn't Survive Contact With Real Data

Personal learning log. Not read by Claude automatically — this is for me, to
recall what I built and why once the project is done. Not needed as context for
future sessions or development; it's a record, not a spec.

BUILD.md's rule for this day was explicit: everything here has to be built
**after** Day 11, so each item gets a real before/after number instead of an
unfalsifiable bullet point. That rule did its job in a way I didn't expect
going in — two of the four planned items turned out to rest on assumptions
Day 11's actual data didn't support, and the only reason I caught that before
shipping either one was because the data existed to check against.

---

## Part 1 — The plan assumed things the eval never found

Before writing any code, I re-read Day 11's own failure-mode analysis against
what BUILD.md assumed Day 11.5 would need it for. Two mismatches, both real:

**Hybrid search's justification wasn't in the data.** BUILD.md frames it as
fixing dense retrieval's weakness on exact-match tokens — identifiers, error
codes, surnames — and points at the Transformer paper as the reason it's
measurable at all. But Day 11's failure-mode section found **zero** failures
of that kind. The technical-doc questions (`sf-09`, `sf-10`, `mh-03`,
`fig-01`) already hit 100%/100% at k=5 on pure vector search, and `sf-10`'s
one real miss was chunking/phrasing drift, not a missed exact token. Hybrid
search got built anyway — the point was to have the honest number either way,
and "a real feature that measured ~0 on this corpus" is a more defensible
portfolio claim than pretending it wasn't tried.

**The abstention threshold can't be the clean cutoff BUILD.md implied.**
"Day 11's data sets the number" reads like the two adversarial questions'
similarity scores would sit visibly below the answerable ones. Checking the
actual numbers: `adv-01` (0.460) and `adv-02` (0.469) land **inside** the
answerable range (0.382–0.693), not below it — several genuinely answerable
questions score lower than both adversarial ones. No single number can
separate them. Decision: a conservative floor set below the lowest answerable
score, documented plainly as *not* catching the adversarial cases, rather
than a threshold tuned to chase a separation that isn't there.

Both of these were catchable only because Day 11 exists and I checked against
it before writing code, not after. That's the whole reason BUILD.md ordered
these two projects the way it did.

---

## Part 2 — Hybrid search: built, measured, confirmed to do nothing here

Migration 009 adds a generated `tsvector` column on `chunks.content` (kept in
sync automatically, no ingestion code touched) plus a GIN index, and
`match_chunks_hybrid` — vector search and Postgres full-text search fused by
Reciprocal Rank Fusion (`1/(60+rank)` per ranker, summed), same
`security invoker` shape as the existing `match_chunks` so RLS keeps doing the
work it's always done.

```sql
with vector_ranked as (
  select c.id, row_number() over (order by c.embedding <=> query_embedding) as rank
  from public.chunks c where c.embedding is not null
  order by rank limit match_count
),
text_ranked as (
  select c.id, row_number() over (
    order by ts_rank(c.content_tsv, websearch_to_tsquery('english', query_text)) desc
  ) as rank
  from public.chunks c
  where c.content_tsv @@ websearch_to_tsquery('english', query_text)
  order by rank limit match_count
)
-- fused by 1/(60+rank), summed across both rankers
```

Re-ran Day 11's retrieval eval with this wired in, before touching reranking
at all: **94% / 94% / 0.74 — byte-identical to the Day 11 baseline at k=5.**
Exactly the null result Part 1 predicted. Confirmed, not assumed: RLS still
holds on the new function (`cross-user` check, PASS, zero overlap), and the
function returns the right shape, but on this corpus it has nothing to win.

---

## Part 3 — Reranking: the one item with a named target, and it hit

`retrieve()` now fetches 20 candidates through the hybrid RPC instead of 5.
`rag.rerank()` (new, wraps a new `ingestion.rerank_documents()` next to the
existing Cohere client) sends those 20 plus the literal question to
`rerank-v4.0-fast`, keeps the best 5.

**`sf-10`, Day 11's one real retrieval miss, moved from rank #7 to rank #3.**
That's the specific, named prediction Day 11's log made about this exact
question, checked and confirmed rather than assumed:

| Stage | Hit rate (k=5) | Recall | MRR |
|---|---|---|---|
| Day 11 baseline | 94% | 94% | 0.74 |
| + hybrid alone | 94% | 94% | 0.74 |
| **+ reranking** | **100%** | **100%** | **0.854** |

`sf-10` is the only question whose hit/miss status flipped between the
hybrid-only and reranked rows — the MRR jump is that one fix plus smaller
rank improvements on questions that were already hits.

**Cost and latency, measured, not guessed.** Rerank is billed per search
($0.002/question for `-fast`), which is *larger* than either model's own
average per-answer cost from Day 11's check 11 (gemini $0.0006, gpt-5.4-nano
$0.0004) — reranking roughly triples-to-quintuples what a question costs on
top of what it already cost. Latency, timed directly against the real Cohere
endpoint (5 calls, 20 candidates each): ~94ms steady-state, with a one-time
~900ms cold start on the client singleton's first use per process. Small
against a multi-second streamed answer, but the dollar cost isn't free, and
a self-funded app with daily caps should know that number rather than assume
"reranking is cheap."

**A model choice worth remembering:** `rerank-v4.0-fast` over `-pro`
($0.002 vs $0.0025/search) — the cheaper one, in keeping with this project's
cost constraint. Not re-measured against `-pro` to see if the accuracy
delta would be worth the extra cost; a real gap to leave open rather than
assume away.

---

## Part 4 — The abstention threshold moved twice, and the second move mattered

First pass, using Day 11's pure-vector numbers: floor at 0.35, safely below
the lowest answerable top-1 similarity seen (0.382). Reasonable — until I
recomputed the same numbers against the *actual* reranked pipeline before
wiring anything in, per the plan's own explicit warning that reranking could
shift this.

It did. **Reranking can promote a chunk with a *lower* raw cosine similarity
than vector search's own top pick**, because it's scoring literal relevance,
not embedding distance — an obvious fact in hindsight that I hadn't actually
priced in until I looked. Post-rerank, the lowest genuinely-answerable
top-1 score dropped to `sf-07` at 0.334. Had I shipped 0.35 as planned, the
app would have wrongly abstained on a real, answerable question the first
time someone asked something like it. Caught by checking the fresh numbers
before wiring the threshold in, not by luck.

Final: `ABSTAIN_THRESHOLD = 0.30`. Adversarial questions (0.45–0.46) still sit
well above it, exactly as Part 1 predicted — this floor was never meant to
catch those, and the code comment says so plainly rather than implying a
precision it doesn't have.

**Live-verified, not just unit-tested:** asked the eval account (Paul Graham
essays + the Attention paper) "What is the best way to season a cast iron
skillet?" through the actual running API. Response: `sources: []`, the fixed
message, near-instant (no multi-second token stream) — confirmation the LLM
call never fired, not just that the branch was reachable.

---

## Part 5 — Prompt injection: the honest finding wasn't the dramatic one

Fixture: a short, real-looking expense-policy document
(`scripts/fixtures/prompt_injection_test.txt`) with a hidden instruction
buried partway through. First version was mild — a bracketed
`[SYSTEM: ...]` line telling the model to append a canary string. Tried it
against gemini before writing any defense: **resisted, clean answer, no
canary string.**

That result was too weak to trust on its own — an undefended app resisting a
soft attempt proves little, and it would make an "after defense, still
resisted" result meaningless (nothing to show the defense actually did
anything). Rewrote the fixture to impersonate a genuinely higher-priority
system directive and try to redirect the user to a fake account-verification
link (`.example.com` — IANA-reserved, never resolves, safe to use as a real
URL string in a real test) instead of answering. Re-tested against **both**
supported models, since the app is explicitly multi-model and a defense that
only holds for one of the two models it ships isn't a defense the app
provides.

**Before any defense code existed: 2/2 models resisted the strong attempt.**
Both gave the correct `$75/day` answer, no phishing redirect, no leaked
instruction. That's the model's own instruction-hierarchy training plus the
existing "ground every claim" system prompt doing the work incidentally —
not anything built for this.

Built the defense anyway — `<<<SOURCE>>>...<<<END SOURCE>>>` delimiters
around every text source in `build_messages`, plus an explicit rule in
`SYSTEM_PROMPT` naming that content as untrusted, "no matter how it is
phrased" — as defense-in-depth against exactly the two things this one clean
result doesn't cover: a future model swap, and a more determined attacker
than two single-shot attempts. Re-tested after: both models still resisted,
and critically, **neither answer's shape changed** — no leaked delimiter
markers, citation still intact. The defense cost nothing measurable and
didn't regress anything.

**What I'm not claiming:** that injection is impossible against this app.
Two escalating single-shot attempts against two models is not exhaustive
red-teaming. What's actually true, stated as such in `eval_results.md`
rather than oversold: the specific realistic attack tried here doesn't work
today, on either supported model, and there's now a structural defense in
place beyond incidental model behavior.

---

## Part 6 — Small things that cost time

- **The retrieve phase ran out of token budget with reranking added.**
  Reranking adds a second network round-trip per question on top of embed +
  retrieve, and 18 questions no longer comfortably fit inside one ~60-second
  Clerk token the way Day 11's version did. 10/18 finished on the first
  attempt; the incremental per-question cache (built for exactly this) picked
  up the remaining 8 cleanly on a second run with a fresh token. Not a bug —
  the caching design existed specifically so a harder, slower pipeline
  degrades to "run it twice" instead of "start over."
- **`_is_cached` needed updating for the new field, or a rerun would have
  silently reused stale pre-Day-11.5 cache entries.** Caught before it
  shipped by tracing through what `reranked5`'s absence on an old entry would
  do downstream (a `KeyError` in the generation phase, much later and much
  less obvious than a cache-completeness check catching it immediately).
- **`Invoke-WebRequest`'s IE-parsing security prompt.** PowerShell 5.1 tries
  to use Internet Explorer's engine to parse response bodies unless told not
  to, and blocks on a confirmation prompt if that engine isn't available —
  `-UseBasicParsing` skips it entirely. Nothing to do with this app; a
  Windows/PowerShell-version gotcha worth remembering for the next local API
  test, same category as Day 11's `curl` vs `curl.exe` note.
- **Pasted a live Clerk token into chat by accident.** Caught immediately,
  flagged, not repeated back — and low actual risk since the token's own
  ~60-second lifetime meant it was already dead by the time it mattered. The
  fix going forward is procedural, not technical: paste command *output*,
  never the token itself, and grab the token as the very last step right
  before running the command, not earlier.

---

## What Day 11.5 cost

One commit on `worktree-day11.5-improvements` (branched off `main`, not
pushed). One new migration (009, hybrid search). Five files touched in
`apps/api` (`rag.py`, `chat.py`, `ingestion.py`, and their call sites),
`scripts/eval.py` updated to exercise the real production pipeline instead of
a stale one, one new eval fixture, one new eval question. Real money: a
handful of Cohere rerank calls (fractions of a cent) plus the LiteLLM calls
already covered by Day 11's spend cap — nothing that needed separate
approval beyond what the plan itself already flagged.

No frontend touched, same as Day 11 — this whole day lives in `apps/api`,
`infra/supabase/migrations`, and `scripts`.

## Still open

- **Nothing pushed yet.** Same standing rule as every prior day: commit
  freely, ask before `git push`. This branch sits local until that
  conversation happens.
- **`rerank-v4.0-pro` vs `-fast` was a cost-driven choice, not a measured
  one.** If a future eval shows accuracy left on the table at this corpus
  size, that's the first thing to re-test.
- **The abstention threshold's honest ceiling is still open, not solved.**
  `adv-01`/`adv-02` need an LLM-as-judge grader on retrieved context to catch
  reliably — a real, named gap, not something 0.30 was ever going to close.
- **Prompt injection's residual risk is explicitly not "solved."** Two
  single-shot attempts against two models is a floor under the claim, not a
  ceiling on it. A more adversarial, iterative red-team pass is future work
  if this app's threat model ever calls for it.
- **Day 12 still needs the README's "what's next" section updated** — hybrid
  search and re-ranking move out of that list now that they're built and
  measured, per BUILD.md's own note about that section.
