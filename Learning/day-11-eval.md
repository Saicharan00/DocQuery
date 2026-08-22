# Day 11 — Turning "It Works" Into a Number

Personal learning log. Not read by Claude automatically — this is for me, to
recall what I built and why once the project is done. Not needed as context for
future sessions or development; it's a record, not a spec.

Every day up to now added something. Day 11 didn't add a feature — it added
**proof**. Ten days of "I tried it and the answer looked right" turned into
eleven measured checks, a failure-mode writeup, and an automated test that
finally settles the one thing this whole project has claimed since Day 1 and
never verified: that one user cannot see another user's documents.

BUILD.md calls this "the interview-gold day" and doubles its length for it —
one day planned became two. That doubling was the right call. This is the
longest single day of the project and the log reflects that.

---

## Part 0 — The rule you obey before writing a single question

Before any code, before any corpus, one rule, stated in BUILD.md in bold:

> Write every question from the **source document**, in your own words, and
> *only then* find the chunk that should answer it.

The tempting shortcut is the opposite order: read the stored chunks, then write
a question per chunk. That's contamination. A question written from a chunk
reuses the chunk's own vocabulary, so retrieval matches for reasons that have
nothing to do with how a real person actually asks — and the hit rate comes
back inflated with **no way to see it happening**, because the inflation and
the real signal look identical from outside.

Real people ask abbreviated, sloppy questions that use a synonym instead of the
document's own words. "give count" from Day 9b is the extreme version of this —
a real question that shares almost no vocabulary with its answer.

Free if obeyed from the start. The alternative — writing 18 questions the easy
way and only noticing the contamination once retrieval scores suspiciously well
— costs a full rewrite. So Part 1 below happened in the order the rule
demands: essays and PDF read first, questions written from memory of what they
said, chunk ids resolved only afterward, by a completely separate phase.

---

## Part 1 — Why this app forces a very specific shape onto the harness

Two decisions made on earlier days turn "write a test script" into "design a
multi-phase pipeline with a filesystem cache." Neither is optional, and both
are called out explicitly in the plan before a line of code exists:

- **RLS is the only security boundary** (Day 1, never relaxed). `rag.retrieve`
  takes no `user_id` argument — it trusts whichever Supabase client calls it,
  and that client's identity comes entirely from the Clerk JWT in its headers.
  There is no service-role bypass anywhere in this codebase, on purpose. So the
  eval script needs a *real* JWT for the test account, the same way a browser
  does — there's no back door for a test harness to use instead.
- **Clerk tokens live ~60 seconds.** Every phase that touches Supabase has to
  finish inside one paste of a token. Everything downstream of that — the paid
  LLM calls, the judging, the report — can take as long as it wants, because
  none of it ever touches Supabase again.

The second constraint is why `eval.py` is not one script but **seven
independently-resumable phases**, each writing its own cache file under
`scripts/eval_cache/`, written incrementally (one atomic write per completed
question) so a slow network or a token dying mid-run loses at most the
question in flight:

```
paste a fresh Clerk token
        │
        ▼
Phase 1: retrieve        <- the ONLY phase racing the 60s clock
        │  retrieval.json, corpus_chunks.json
        ▼
Phase 0: resolve-hints    <- human confirms ground-truth chunk ids
        │  ground_truth_chunk_id written into eval_qa.json
        ▼
Phase 2a: generate        <- 72 paid LLM calls, no Supabase, unhurried
        │  generations.json
        ▼
Phase 2b: judge           <- RAGAS + LLM-judge scoring
        │  judgments.json
        ▼
Phase 2c: sample-for-human  <- pick 10, hand-score blind
        │  judge_validation.json
        ▼
Phase 3: report            <- pure local read, zero network calls
        │  eval_results.md
```

The numbering (Phase 0 sits *after* Phase 1 in the pipeline but is called
"Phase 0" because it produces the ground truth everything else needs) is a
naming leftover from the plan, not a bug.

**Why this was even possible in one script:** `rag.py`'s functions are plain
Python — no FastAPI, no request object — a decision made back on Day 7
specifically so "an ablation that has to boot a web server to measure
retrieval is an ablation nobody runs twice." Day 11 is the day that
decision's whole payoff arrived. `eval.py` imports `embed_query`, `retrieve`,
`load_images`, `rewrite_query`, `build_messages`, `stream_answer` straight out
of the same module `/chat` calls — the eval harness is measuring the actual
production code path, not a reimplementation of it.

---

## Part 2 — The corpus and the 18 questions

Five documents in one fresh test account:

- **4 Paul Graham essays** (`alien.html`, `do.html`, `winc.html`, `hubs.html`)
  — plain prose, public, unambiguous. Good for straightforward and multi-hop
  questions with nothing exotic in them.
- **1 technical document** — `attention_is_all_you_need.pdf`, the Transformer
  paper. Chosen specifically because it has **both figures and identifiers**
  (BLEU scores, GPU counts, table numbers) — it's the only source in the corpus
  for the figure-only question, and the only thing hybrid search (Day 11.5)
  will have anything to win on, since the essays are pure prose with no exact
  tokens to out-match a dense vector on.

18 hand-written pairs in `scripts/eval_qa.json`, in the mix BUILD.md specifies:
10 straightforward, 3 multi-hop (needing 2+ chunks), 2 adversarial (correct
answer is "not in the documents"), 2 multi-turn (follow-ups that only make
sense given a prior turn — these exist specifically to grade Day 9b's rewrite
work), 1 figure-only (answerable only from an image — grading whether Day 6b's
images ever actually come back out).

Extraction fidelity and the corpus itself pulled in a few extra test documents
beyond the five real ones (a scanned PDF, a table-heavy DOCX fixture) purely to
have something broken to point the extraction-fidelity check at — see Part 9.

---

## Part 3 — Phase 1: retrieve, and the twin function built just to be slow

Check #2 on BUILD.md's list ("ANN recall vs exact search") needs a **true**
nearest-neighbor result to compare the production search against. The
production function, `match_chunks` (migration 005), is fast because of an
HNSW index — and HNSW is an **approximate** nearest-neighbor structure. It can
return the 4th-closest chunk while silently missing the true 3rd. There's no
per-call flag that turns the approximation off.

So: migration 008, `match_chunks_exact` — a byte-for-byte copy of
`match_chunks`'s shape with one line added, `set enable_indexscan = off`,
forcing Postgres onto a full sequential scan that checks every single chunk.
Slow on purpose. It exists only to be `match_chunks`'s ground truth, never to
be called from a real request.

```sql
-- Everything else is a deliberate copy of 005: same columns, same
-- `security invoker` (RLS still applies), same grant shape.
create function public.match_chunks_exact(...)
...
set enable_indexscan = off
as $$
  select ... order by c.embedding <=> query_embedding limit match_count;
$$;
```

**In plain English:** the app normally finds similar text with a fast
shortcut, like a librarian who knows roughly where things are instead of
checking every book. That shortcut can occasionally guess wrong. This function
is the slow way — check every book — so there's a known-correct answer to
measure the fast way against.

`retrieve` calls both functions once per question at `match_count=10`, so the
k=3/5/10 ablation in the report is just three different slices of the same 10
rows — no extra round trips, which matters because this whole phase is racing
a 60-second clock.

**The risk that had to be checked, not assumed:** Day 10a had already measured
`embed_query` taking 7.7 seconds cold. Add 18 questions × 2 RPC calls plus a
few real `rewrite_query` LLM calls for the multi-turn questions, and a naive
sequential run gets uncomfortably close to 60 seconds. The actual run: **18/18
questions cached in 8.5 seconds, off a token that had 21 seconds left when it
started.** The thread-pool concurrency and incremental caching held up in
practice, not just on paper.

---

## Part 4 — Phase 0: a human has to look at the data once

"Was the right chunk retrieved" needs a real chunk id to compare against. The
hint strings I wrote (`"the passage discussing X"`) are for a human to
recognize, not for a script to match automatically — guessing wrong here
would quietly poison every retrieval metric built on top of it. So
`resolve-hints` shows candidate chunks (simple keyword-scored against the
hint) for each of the 16 non-adversarial questions, and I confirm or correct
its guess by hand. Nothing after this phase needs a human again.

**A bug found immediately:** image chunks store only a page-label
(`"[Image from page 5]"`) as their `content` — so they scored **0** in
keyword ranking against any hint and could never surface as a candidate,
which would have made the figure-only question (`fig-01`) impossible to
resolve. Fixed by special-casing `type == "figure-only"` questions to show
every image chunk from that document first, bypassing the keyword score
entirely.

**What resolving all 16 by hand actually showed:** `mh-02` and `mh-03`
genuinely needed two separate chunks, matching their multi-hop design. But
`mh-01`, `mt-01`, and `mt-02` — despite hints suggesting they'd need two —
only needed **one**, because the source essays are short enough that one
chunk covers both facts a "multi-hop" question was designed to require. The
corpus being small enough to make some intended multi-hop questions
single-hop in practice is a real property of this test corpus, not a flaw in
the questions.

---

## Part 5 — What retrieval actually measured

| k | Hit rate | Recall | MRR |
|---|---|---|---|
| 3 | 94% | 91% | 0.74 |
| 5 (production default) | 94% | 94% | 0.74 |
| 10 | 100% | 100% | 0.75 |

| Type | n | Hit rate | Recall | MRR |
|---|---|---|---|---|
| straightforward | 10 | 90% | 90% | 0.73 |
| multi-hop | 3 | 100% | 100% | 0.67 |
| multi-turn | 2 | 100% | 100% | 1.00 |
| figure-only | 1 | 100% | 100% | 0.50 |

And the ANN-vs-exact comparison check #2 exists for: **10/10 overlap on every
one of 18 questions, same order.** HNSW cost nothing at this corpus size — a
real, checked fact rather than an assumption carried forward from "it's just
an index, it should be fine."

**Note deliberately left out of the metrics:** precision@k isn't reported.
With exactly one correct chunk per question, even *perfect* retrieval scores
1/5 = 20% precision — the metric is structurally capped at a number that looks
bad regardless of quality, so it says nothing and was left out on purpose
rather than reported and then explained away.

The one real miss — `sf-10`, ground truth ranked #7 at k=5 — is where
retrieval numbers stop being the whole story and Part 10 picks it up.

---

## Part 6 — Phase 2a: generate, and a rate limit that only shows up at scale

72 calls: 18 questions × 2 models (`gemini/gemini-3.5-flash-lite`,
`gpt-5.4-nano`) × 2 conditions (`rag`, using the cached top-5 chunks; `no_rag`,
using none — the comparison row that turns "it answered" into "retrieval is
what made it answer correctly"). Reuses `build_messages` and `stream_answer`
verbatim, so this is the same call shape `/chat` makes.

Cost estimate printed and confirmed before firing a single paid call, same
spend-cap philosophy the app's own `_enforce_daily_limit` already uses.

**Rate limit found on the first real run:** Gemini's free tier caps at 15
requests/minute per model. `generate` fires sequentially with no pacing, so
anything past question 15 or so failed outright on the first attempt — 20
calls lost. Fixed with an automatic retry on a rate-limit error: **65-second
backoff**, not the ~29 seconds the error message itself suggested, because
that number is time remaining in the window *at the moment of failure* — a
full window plus margin is the number that's actually safe. Reran clean.

**Real spend: $0.0353** across all 72 calls (worst-case estimate going in was
$0.16). Cost estimated client-side via `litellm.cost_per_token`, since a
streamed response never hands back a provider `usage` block to read the real
number from.

| Model | RAG correctness | no-RAG correctness |
|---|---|---|
| gemini/gemini-3.5-flash-lite | 4.56/5 | 2.00/5 |
| gpt-5.4-nano | 4.56/5 | 1.28/5 |

That gap — 4.56 with retrieval vs. 1.28–2.00 without — is the actual payoff
number for the entire RAG pipeline. Everything from Day 5 through Day 10
exists to produce that gap.

---

## Part 7 — Phase 2b: judge, and the version of RAGAS that doesn't import

This phase is where the day's real engineering fight happened, and none of it
was in `eval.py`'s own logic — it was getting RAGAS to run at all.

**Bug 1 — `uv add ragas` resolves a version that can't even be imported.**
`ragas==0.4.3` unconditionally imports
`langchain_community.chat_models.vertexai` at module load time, and that
submodule was removed from newer `langchain-community` releases. `import
ragas` failed before any of my own code ran — a known, open upstream bug, not
a mistake in this project. Fixed by pinning `ragas==0.3.9` +
`langchain-community<0.4` in `pyproject.toml`.

**Bug 2 — the "obvious" integration point is a dead end.**
`ragas.llms.llm_factory(provider="litellm")` looks like exactly the adapter
needed, and isn't: it returns an `InstructorBaseRagasLLM`, and
`Faithfulness`/`AnswerRelevancy` require a real `BaseRagasLLM` subclass. A
small custom wrapper genuinely was required — the plan's original guess was
right, just not for the reason expected going in. Built `LiteLLMRagasLLM`
(`generate_text`/`agenerate_text` around `litellm.completion`/`acompletion`)
and, once `LiteLLMEmbeddings` turned out to implement the *new* RAGAS
embeddings interface while `AnswerRelevancy` calls the *old* one
(`embed_query`/`embed_documents` from LangChain's base class — an internal
version-transition gap inside ragas 0.3.9 itself), a matching
`LiteLLMRagasEmbeddings` wrapper too.

Three independently-resumable scoring passes, `rag`-only where a metric
requires retrieved context to score against:

- **Faithfulness + answer relevancy** (RAGAS) — does the answer stick to what
  was retrieved, does it actually address the question asked.
- **Correctness** — a judge model *above* the two models being judged
  (`gpt-5.4`), 1–5 against the hand-written ground truth answer. Run on **all
  72** generations, both conditions — a `no_rag` answer's correctness is still
  meaningful signal.
- **Citation accuracy** — every `[n]` in an answer, checked sentence-by-
  sentence against the actual untruncated source text it cites. Skipped
  entirely (no call made) for an answer with no citations to check, which is
  most `no_rag` answers and every honest refusal.

| Model | Faithfulness | Answer relevancy |
|---|---|---|
| gemini/gemini-3.5-flash-lite | 0.84 | 0.65 |
| gpt-5.4-nano | 0.85 | 0.76 |

**Citation accuracy overall: 76/81 sentences supported (94%)** — 92% Gemini,
97% GPT.

Real spend on this phase: ~$1.16 (72 correctness + 36 citation + 36 RAGAS
calls across two runs — the first run failed all 36 RAGAS calls on Bug 1
above *before* spending on them, so nothing was wasted on the failure itself).

---

## Part 8 — Phase 2c: is the judge even trustworthy?

Check #8's correctness score is one model's opinion, reported as if it were
fact. Check #10 exists to find out whether that's a safe thing to do:
`sample-for-human` picks 10 judged answers deterministically (seeded random,
so reruns don't orphan a half-finished hand-scoring pass) and writes them with
a blank `human_score` field *ordered before* the judge's own score and
reasoning — so scoring happens blind, before seeing what the machine thought.

**Result: 9/10 exact agreement, 10/10 within one point.** The one
disagreement is worth keeping in full, because it's a real, specific,
explainable case rather than noise:

> **`mh-03`, gpt-5.4-nano.** The paper's actual claim is that the Transformer
> trained faster than prior models, backed by the **English-to-French** big
> model hitting 41.8 BLEU after 3.5 days on 8 GPUs. The model's answer stated
> the parallelizability claim correctly, but attached that same "3.5 days on
> 8 P100 GPUs" figure to the **English-to-German** result instead — right
> general claim, wrong specific number attached to it. I scored it 4 (the
> general claim is right); the judge scored 3 (the cited support is factually
> wrong). The judge's reasoning was more precise than my own first read.

**Conclusion:** the correctness numbers in the report are trustworthy enough
to report as-is, with this one case as an honest, specific caveat rather than
a vague "judges can be imperfect" disclaimer.

---

## Part 9 — Extraction fidelity: read the parser's output before trusting anything downstream

BUILD.md is explicit that this check comes **first**, before any retrieval
number is trusted, because a chunk that was garbage when written poisons
every metric built on top of it, silently. Four documents, read by eye:

| Document | Score | What was actually wrong |
|---|---|---|
| `attention_is_all_you_need.pdf` (2-column academic PDF) | degraded | Prose reading order is correct — the parser's page-region reading isn't as naive as the code comment worried it might be. But **Table 2** loses its column alignment: header and data rows flatten into one list, so a row like "ByteNet [18] / 23.75" no longer says which language pair 23.75 belongs to. |
| `major project-LAST2.3nishanth updated.pdf` (91-page table-heavy report) | degraded | Inconsistent **by table**, not by page. Simple fully-populated tables extract cleanly, row-major, readable as-is. But one table (IC7805 specs) gets pulled entirely out of its visual position — its content appears in the raw text *after* an unrelated paragraph, disconnected from its own label two paragraphs earlier. |
| `Non-text-searchable.pdf` (scanned page) | clean, by design | `_parse_pdf` returned 0 characters. Confirmed with `page.get_image_info()` that the page really is one embedded image with no text layer at all — not a bug, and exactly the signal Day 6b's scanned-PDF exemption relies on to fall back to image-only ingestion instead of failing the upload. |
| `table_test_fixture.docx` (5 table shapes) | unusable | `_parse_docx` joins `paragraph.text` only. **0 of 5 tables survive** — every number, date, SKU, and percentage inside them is silently dropped; only the prose describing what each table contains comes through. Confirms the code's own existing comment: `# ponytail: paragraphs only — text inside tables is skipped.` |

Nothing here was a surprise in direction — both `ponytail:` comments in
`ingestion.py` already said this would happen. What this check adds is that
it's now **measured and written down**, with real documents and real broken
output quoted, instead of a comment nobody has actually confirmed against a
file.

---

## Part 10 — Failure mode analysis: the section interviewers actually read

BUILD.md calls this out by name as the part that matters most, and it's the
one section that can't be computed — it has to be read out of the actual
cached judgments by hand.

**The one real retrieval miss — `sf-10`.** Ground-truth chunk ranked #7, not
top-5. But a *neighboring* chunk (one of `mh-03`'s own two ground-truth
chunks, as it happens) ranked #1 and contained the same training-time fact, so
generation still answered correctly. Root cause: 800-token/100-overlap
chunking spreads one fact across adjacent chunks, and the phrasing "how
long... and on what hardware" happened to embed closer to the neighbor than to
the chunk Phase 0 picked as canonical. Not a bug — an artifact of chunk
overlap plus a single-ground-truth-chunk label. Re-ranking (Day 11.5) is the
direct fix, since it re-scores the full k=20 set against the literal query
rather than trusting embedding proximity alone.

**Two real generation failures**, both with retrieval and rewriting working
correctly:

- **`mt-01`** (multi-turn) — the rewritten query was clean
  (`"...which of the three things Graham says we should do deserves the most
  emphasis, and why?"`), and the correct chunk was retrieved at rank 1. Both
  models still failed: Gemini said *"the sources do not state that any one
  deserves the most emphasis"* — a false refusal, because the passage argues
  *why* "make good new things" is special without using those literal words.
  GPT just repeated the earlier turn's answer, ignoring the actual follow-up.
  A synthesis/reading-comprehension gap in generation, not anything upstream.
- **`fig-01`** (figure-only), Gemini only — named the wrong two blocks in the
  architecture diagram ("Add & Norm", "Feed Forward" instead of "Linear",
  "Softmax"). GPT got it right on the same retrieved image. Confirms the
  known image-retrieval ceiling from Day 10c: the right figure gets retrieved,
  but reading it correctly is still on the model, and one of two vision
  models misread it.

**Two apparent failures that were actually judge/metric artifacts**, caught
only by reading the raw judge output instead of trusting the score:

- **Citation accuracy under-scores multi-source sentences.** Every
  "unsupported" verdict in the per-question table (`sf-06` 0/2, `mh-03` 1/3)
  turned out to be a sentence that legitimately synthesizes facts from *two*
  cited chunks jointly — e.g. sf-06's tax-rate sentence cites `[1],[2]`
  because the federal+state numbers come from one chunk and the wealth-tax
  conversion from the other. The citation judge checks whether a sentence is
  supported by its citations *individually*, and never credits a sentence
  whose support is legitimately split across them. Same run, same judge —
  `mh-03`'s third sentence, single-cited, scored `supported: true`. A
  methodology gap in check 9, not evidence of a hallucinated source.
- **`sf-07`'s faithfulness = 0.00 despite a near-verbatim, citation-verified
  answer.** Gemini's answer about Sean Parker is close to word-for-word out of
  the retrieved chunk, and its own citation was separately checked and marked
  `supported: true`. RAGAS scoring the same answer's faithfulness at 0.00 —
  while correctness (5) and citation accuracy (supported) both say it's
  grounded — reads as RAGAS's claim-decomposition producing noise on this
  particular answer, not a real ungrounded response. Worth remembering:
  **never trust one metric in isolation** — it's exactly why checks 7, 8, and
  9 are three separate rows instead of one blended "quality" score.

> Retrieval is not this app's bottleneck — 16/16 real hit rate at k=5. The two
> real failures are both generation-side, and the multi-turn question this
> whole eval was built to stress-test turned out to have its rewriting and
> retrieval work fine. The residual failure moved one layer downstream, into
> generation itself.

---

## Part 11 — Cross-user isolation: the claim this project made on Day 1, finally checked

Since Day 1, `messages_isolation` and `chunks_isolation` have been the
sentence "RLS is the security boundary." Day 7's own learning log flagged it
explicitly: `match_chunks` is `security invoker`, so the policy *should* apply
inside it — but that's an argument, never a test. It rode through Days 7, 8,
9a, 9b, and 10a as an open item, each day's log repeating the same line.

Day 11 closes it with an automated check, added to `eval.py` as a new
`cross-user` subcommand — deliberately outside the 7-phase pipeline above,
since it needs two live tokens from two genuinely different accounts at once
rather than one:

```python
chunks_a = rag.retrieve(supabase_a, query_vector, k=MATCH_COUNT)
chunks_b = rag.retrieve(supabase_b, query_vector, k=MATCH_COUNT)

leaked = {c["id"] for c in chunks_a} & {c["id"] for c in chunks_b}
if leaked:
    print("FAIL: ...")
```

**The design choice worth remembering:** the test doesn't require account B to
be empty. It only asserts **zero chunk-id overlap** between what two
different, real, RLS-scoped `retrieve()` calls return for the *same query
vector*. That's a stronger, more general check than "B gets nothing back" —
it holds regardless of whether B has their own unrelated documents, and it
rules out the trivial failure mode where the test would pass for the boring
reason that B simply has nothing to retrieve.

Run against two real Clerk accounts — the eval test account, and a second,
genuinely separate account signed in from a second browser session:

```
PASS: user A's retrieve() returned 10 chunk(s) on this query;
user B's retrieve() on the identical vector returned 10, none of them A's.
```

**Two populated, non-empty result sets, on the identical query vector, with
zero overlap** is a stronger result than an empty-B pass would have been — it
rules out "B just has nothing to find" as the trivial explanation and shows
RLS actively partitioning two real, populated corpora. The one item this
project has claimed and never proven since Day 1 is now a checked fact.

---

## Part 12 — Small things that cost time

- **Windows' console defaults to cp1252** and crashes printing "Erdős" —
  `sys.stdout.reconfigure(encoding="utf-8")` near the top of `eval.py` fixed
  it once, for every phase, rather than sanitizing text on the way out.
- **A process/edit race.** Hand-editing `eval_qa.json` while a phase's script
  is still running in another terminal gets silently clobbered by that
  process's next incremental save — it holds its own in-memory copy from
  when it started and has no idea the file changed underneath it. Always let
  the process exit before touching its own output file directly.
- **The test account's documents have generic upload names**
  (`Paul Graham-1.txt` through `-4.txt`), not their source filenames. The real
  mapping had to be confirmed by hand by actually reading the uploaded files,
  then hardcoded in `eval.py` as `SOURCE_DOC_TO_DOCUMENT_NAME`.
- **A cost-estimate bug that would have hidden real spend.** The "RAGAS ≈ 5x
  an average correctness call" approximation divided by the count of
  *pending* correctness calls — which was zero on a rerun, since correctness
  was already fully cached — silently showing an estimate of $0.00 before a
  ~$0.49 rerun. Fixed to average over all 72 generations, not just the ones
  still pending.

---

## What Day 11 cost

Twelve commits on `day11-eval` (not counting this log). One new SQL function
(`match_chunks_exact`, migration 008). One new script, `scripts/eval.py`, now
~1500 lines — genuinely the largest single file in the project, and the only
one whose whole job is to measure the rest of it. One dependency pin
(`ragas==0.3.9`, `langchain-community<0.4`) to work around an open upstream
bug. Real money spent: **$0.0353** generation + **~$1.16** judging ≈ **$1.20
total** to produce every number in `eval_results.md`.

No frontend touched. No new endpoint. The entire day lives in `scripts/` and
`infra/supabase/migrations/`, which is exactly right for a day whose whole
purpose is measuring what already shipped, not shipping something new.

## Still open

- **Day 11.5 is next** — re-ranking, hybrid search, a prompt-injection test,
  and an abstention threshold, each built *after* this day specifically so
  each one gets a real before/after number instead of an unfalsifiable
  bullet point ("I added re-ranking" proves nothing on its own).
- **`sf-10`'s retrieval miss is exactly what re-ranking should fix** — a
  concrete, named prediction Day 11.5 can check itself against.
- **Nothing on `day11-eval` has been pushed yet.** Fourteen commits sit local,
  same standing rule as every prior day: commit freely, ask before `git push`.
- **The ordinal-rewrite ceiling from Day 9b** ("the third one" resolving to
  the second item) never showed up as a failure in these 18 questions — none
  of the multi-turn questions happened to test an ordinal. Still an open,
  named limitation; just not one this particular eval run happened to catch.
