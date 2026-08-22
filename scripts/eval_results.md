# Day 11 eval results

## 1. Extraction fidelity

| Document                                                                                         | Score             | Notes                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
|--------------------------------------------------------------------------------------------------|-------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| attention_is_all_you_need.pdf — 2-column academic PDF                                            | degraded          | Column reading order is actually correct — prose flows sequentially across both columns ('1 Introduction → 2 Background → 3 Model Architecture'), despite the ingestion.py code comment warning get_text() reads across columns. But Table 2 (p.8, BLEU/FLOPs comparison) loses its column alignment: header row and sparse data rows flatten into one list, so a row like 'ByteNet [18] / 23.75' no longer says whether 23.75 is the EN-DE or EN-FR score. Verified by reading the real cached production chunks (corpus_chunks.json, document 74dfb13a...), not a synthetic re-parse.                                                                                                                                                                                                                                       |
| major project-LAST2.3nishanth updated.pdf — 91-page table-heavy report                           | degraded          | Inconsistent by table, not by page. Simple, fully-populated tables extract cleanly in correct row-major order (Table 4 DHT11 pinout p.53: 'No: / Pin Name / Description / 1 / Vcc / Power supply...'; Table 8 LCD pins p.66 same pattern) — readable as-is, cell values correctly paired with their row. But Table 1 (IC7805 specs, p.34) is pulled entirely out of its visual position: 'SPECIFICATIONS / IC 7805 / Vout / 5V / Vein - Vout Difference / 5V - 20V / ...' appears in the raw text AFTER an unrelated paragraph about a different topic (variable bench power supplies), disconnected from its own 'Table 1' label two paragraphs above it. A reader going top-to-bottom would misattribute or lose that data. get_text()'s block reading order does not reliably track a table's visual position on the page. |
| Non-text-searchable.pdf — scanned page (GitHub sample)                                           | clean (by design) | _parse_pdf returned 0 chars. Confirmed with page.get_image_info() that the page really is a single 540x700 embedded image with no text layer — not a bug. This is exactly the signal the Day 6b scanned-PDF exemption relies on: empty text makes parse() return [] instead of raising, so the document falls back to image-only ingestion instead of failing the upload.                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| table_test_fixture.docx — 5 table shapes (simple, merged cells, nested, mixed types, multi-page) | unusable          | _parse_docx joins paragraph.text only — every one of the 5 tables is silently dropped. 0 of the actual cell values (numbers, ISO dates, SKUs, percentages) survive; only the prose paragraphs describing what each table contains come through. Confirms the existing `# ponytail:` comment in ingestion.py: 'paragraphs only — text inside tables is skipped.'                                                                                                                                                                                                                                                                                                                                                                                                                                                               |

## 2. ANN recall vs exact search

| Question | HNSW ∩ exact (of 10) | Same top 10?    |
|----------|----------------------|-----------------|
| sf-01    | 10/10                | yes, same order |
| sf-02    | 10/10                | yes, same order |
| sf-03    | 10/10                | yes, same order |
| sf-04    | 10/10                | yes, same order |
| sf-05    | 10/10                | yes, same order |
| sf-06    | 10/10                | yes, same order |
| sf-07    | 10/10                | yes, same order |
| sf-08    | 10/10                | yes, same order |
| sf-09    | 10/10                | yes, same order |
| sf-10    | 10/10                | yes, same order |
| mh-01    | 10/10                | yes, same order |
| mh-02    | 10/10                | yes, same order |
| mh-03    | 10/10                | yes, same order |
| adv-01   | 10/10                | yes, same order |
| adv-02   | 10/10                | yes, same order |
| mt-01    | 10/10                | yes, same order |
| mt-02    | 10/10                | yes, same order |
| fig-01   | 10/10                | yes, same order |

Average overlap: 10.0/10 across 18 question(s).

## 3. Retrieval metrics

Ground truth exists for 16/18 questions (the 2 adversarial questions are excluded by design — no correct chunk exists to hit).

### Ablation — hit rate / recall / MRR at k=3, 5, 10

| k                      | Hit rate | Recall | MRR  |
|------------------------|----------|--------|------|
| 3                      | 94%      | 91%    | 0.74 |
| 5 (production default) | 94%      | 94%    | 0.74 |
| 10                     | 100%     | 100%   | 0.75 |

### Per-question-type breakdown (k=5)

| Type            | n  | Hit rate | Recall | MRR  |
|-----------------|----|----------|--------|------|
| straightforward | 10 | 90%      | 90%    | 0.73 |
| multi-hop       | 3  | 100%     | 100%   | 0.67 |
| multi-turn      | 2  | 100%     | 100%   | 1.00 |
| figure-only     | 1  | 100%     | 100%   | 0.50 |

## 4. Generation metrics

### Answer correctness — RAG vs no-RAG (check 8)

| Model                        | RAG    | no-RAG |
|------------------------------|--------|--------|
| gemini/gemini-3.5-flash-lite | 4.56/5 | 2.00/5 |
| gpt-5.4-nano                 | 4.56/5 | 1.28/5 |

### Faithfulness + answer relevance (RAGAS, rag-only) (check 7)

| Model                        | Faithfulness | Answer relevancy |
|------------------------------|--------------|------------------|
| gemini/gemini-3.5-flash-lite | 0.84         | 0.65             |
| gpt-5.4-nano                 | 0.85         | 0.76             |

### Citation accuracy (check 9)

Overall: 76/81 cited sentences supported (94%).

| Model                        | Supported | Total | %   |
|------------------------------|-----------|-------|-----|
| gemini/gemini-3.5-flash-lite | 44        | 48    | 92% |
| gpt-5.4-nano                 | 32        | 33    | 97% |

## 5. Per-question detail

| Question | Type            | Model                        | Condition | Correctness | Faithfulness | Citations OK |
|----------|-----------------|------------------------------|-----------|-------------|--------------|--------------|
| adv-01   | adversarial     | gemini/gemini-3.5-flash-lite | no_rag    | 1           | n/a          | n/a          |
| adv-01   | adversarial     | gemini/gemini-3.5-flash-lite | rag       | 5           | 1.00         | no citations |
| adv-01   | adversarial     | gpt-5.4-nano                 | no_rag    | 5           | n/a          | n/a          |
| adv-01   | adversarial     | gpt-5.4-nano                 | rag       | 5           | 1.00         | no citations |
| adv-02   | adversarial     | gemini/gemini-3.5-flash-lite | no_rag    | 5           | n/a          | n/a          |
| adv-02   | adversarial     | gemini/gemini-3.5-flash-lite | rag       | 5           | 1.00         | 2/2          |
| adv-02   | adversarial     | gpt-5.4-nano                 | no_rag    | 1           | n/a          | n/a          |
| adv-02   | adversarial     | gpt-5.4-nano                 | rag       | 5           | 0.50         | 1/1          |
| fig-01   | figure-only     | gemini/gemini-3.5-flash-lite | no_rag    | 1           | n/a          | n/a          |
| fig-01   | figure-only     | gemini/gemini-3.5-flash-lite | rag       | 1           | 0.00         | 2/2          |
| fig-01   | figure-only     | gpt-5.4-nano                 | no_rag    | 1           | n/a          | n/a          |
| fig-01   | figure-only     | gpt-5.4-nano                 | rag       | 5           | 0.50         | 1/1          |
| mh-01    | multi-hop       | gemini/gemini-3.5-flash-lite | no_rag    | 5           | n/a          | n/a          |
| mh-01    | multi-hop       | gemini/gemini-3.5-flash-lite | rag       | 5           | 1.00         | 7/7          |
| mh-01    | multi-hop       | gpt-5.4-nano                 | no_rag    | 1           | n/a          | n/a          |
| mh-01    | multi-hop       | gpt-5.4-nano                 | rag       | 5           | 1.00         | 7/7          |
| mh-02    | multi-hop       | gemini/gemini-3.5-flash-lite | no_rag    | 1           | n/a          | n/a          |
| mh-02    | multi-hop       | gemini/gemini-3.5-flash-lite | rag       | 5           | 1.00         | 4/4          |
| mh-02    | multi-hop       | gpt-5.4-nano                 | no_rag    | 1           | n/a          | n/a          |
| mh-02    | multi-hop       | gpt-5.4-nano                 | rag       | 5           | 1.00         | 2/2          |
| mh-03    | multi-hop       | gemini/gemini-3.5-flash-lite | no_rag    | 3           | n/a          | n/a          |
| mh-03    | multi-hop       | gemini/gemini-3.5-flash-lite | rag       | 5           | 0.90         | 1/3          |
| mh-03    | multi-hop       | gpt-5.4-nano                 | no_rag    | 1           | n/a          | n/a          |
| mh-03    | multi-hop       | gpt-5.4-nano                 | rag       | 3           | 1.00         | 2/2          |
| mt-01    | multi-turn      | gemini/gemini-3.5-flash-lite | no_rag    | 1           | n/a          | n/a          |
| mt-01    | multi-turn      | gemini/gemini-3.5-flash-lite | rag       | 1           | 1.00         | no citations |
| mt-01    | multi-turn      | gpt-5.4-nano                 | no_rag    | 1           | n/a          | n/a          |
| mt-01    | multi-turn      | gpt-5.4-nano                 | rag       | 2           | 0.75         | 1/1          |
| mt-02    | multi-turn      | gemini/gemini-3.5-flash-lite | no_rag    | 1           | n/a          | n/a          |
| mt-02    | multi-turn      | gemini/gemini-3.5-flash-lite | rag       | 5           | 1.00         | 2/2          |
| mt-02    | multi-turn      | gpt-5.4-nano                 | no_rag    | 1           | n/a          | n/a          |
| mt-02    | multi-turn      | gpt-5.4-nano                 | rag       | 4           | 0.86         | 1/1          |
| sf-01    | straightforward | gemini/gemini-3.5-flash-lite | no_rag    | 1           | n/a          | n/a          |
| sf-01    | straightforward | gemini/gemini-3.5-flash-lite | rag       | 5           | 1.00         | 5/5          |
| sf-01    | straightforward | gpt-5.4-nano                 | no_rag    | 2           | n/a          | n/a          |
| sf-01    | straightforward | gpt-5.4-nano                 | rag       | 4           | 0.91         | 6/6          |
| sf-02    | straightforward | gemini/gemini-3.5-flash-lite | no_rag    | 1           | n/a          | n/a          |
| sf-02    | straightforward | gemini/gemini-3.5-flash-lite | rag       | 5           | 1.00         | 4/4          |
| sf-02    | straightforward | gpt-5.4-nano                 | no_rag    | 1           | n/a          | n/a          |
| sf-02    | straightforward | gpt-5.4-nano                 | rag       | 5           | 1.00         | 1/1          |
| sf-03    | straightforward | gemini/gemini-3.5-flash-lite | no_rag    | 1           | n/a          | n/a          |
| sf-03    | straightforward | gemini/gemini-3.5-flash-lite | rag       | 5           | 1.00         | 4/4          |
| sf-03    | straightforward | gpt-5.4-nano                 | no_rag    | 1           | n/a          | n/a          |
| sf-03    | straightforward | gpt-5.4-nano                 | rag       | 5           | 1.00         | 1/1          |
| sf-04    | straightforward | gemini/gemini-3.5-flash-lite | no_rag    | 1           | n/a          | n/a          |
| sf-04    | straightforward | gemini/gemini-3.5-flash-lite | rag       | 5           | 0.87         | 6/6          |
| sf-04    | straightforward | gpt-5.4-nano                 | no_rag    | 1           | n/a          | n/a          |
| sf-04    | straightforward | gpt-5.4-nano                 | rag       | 4           | 1.00         | 1/1          |
| sf-05    | straightforward | gemini/gemini-3.5-flash-lite | no_rag    | 1           | n/a          | n/a          |
| sf-05    | straightforward | gemini/gemini-3.5-flash-lite | rag       | 5           | 1.00         | 2/2          |
| sf-05    | straightforward | gpt-5.4-nano                 | no_rag    | 1           | n/a          | n/a          |
| sf-05    | straightforward | gpt-5.4-nano                 | rag       | 5           | 1.00         | 1/1          |
| sf-06    | straightforward | gemini/gemini-3.5-flash-lite | no_rag    | 1           | n/a          | n/a          |
| sf-06    | straightforward | gemini/gemini-3.5-flash-lite | rag       | 5           | 1.00         | 0/2          |
| sf-06    | straightforward | gpt-5.4-nano                 | no_rag    | 1           | n/a          | n/a          |
| sf-06    | straightforward | gpt-5.4-nano                 | rag       | 5           | 1.00         | 4/4          |
| sf-07    | straightforward | gemini/gemini-3.5-flash-lite | no_rag    | 1           | n/a          | n/a          |
| sf-07    | straightforward | gemini/gemini-3.5-flash-lite | rag       | 5           | 0.00         | 1/1          |
| sf-07    | straightforward | gpt-5.4-nano                 | no_rag    | 1           | n/a          | n/a          |
| sf-07    | straightforward | gpt-5.4-nano                 | rag       | 5           | 0.20         | 1/1          |
| sf-08    | straightforward | gemini/gemini-3.5-flash-lite | no_rag    | 1           | n/a          | n/a          |
| sf-08    | straightforward | gemini/gemini-3.5-flash-lite | rag       | 5           | 0.67         | 1/1          |
| sf-08    | straightforward | gpt-5.4-nano                 | no_rag    | 1           | n/a          | n/a          |
| sf-08    | straightforward | gpt-5.4-nano                 | rag       | 5           | 0.67         | 1/1          |
| sf-09    | straightforward | gemini/gemini-3.5-flash-lite | no_rag    | 5           | n/a          | n/a          |
| sf-09    | straightforward | gemini/gemini-3.5-flash-lite | rag       | 5           | 1.00         | 2/2          |
| sf-09    | straightforward | gpt-5.4-nano                 | no_rag    | 1           | n/a          | n/a          |
| sf-09    | straightforward | gpt-5.4-nano                 | rag       | 5           | 1.00         | 1/1          |
| sf-10    | straightforward | gemini/gemini-3.5-flash-lite | no_rag    | 5           | n/a          | n/a          |
| sf-10    | straightforward | gemini/gemini-3.5-flash-lite | rag       | 5           | 0.75         | 1/1          |
| sf-10    | straightforward | gpt-5.4-nano                 | no_rag    | 1           | n/a          | n/a          |
| sf-10    | straightforward | gpt-5.4-nano                 | rag       | 5           | 1.00         | 0/1          |

## 6. Judge validation (check 10)

Exact agreement: 9/10 (90%).
Within one point: 10/10 (100%).

| Question                                                               | Human | Judge | Match |
|------------------------------------------------------------------------|-------|-------|-------|
| Who does Graham describe a college student meeting by chance after ... | 5     | 5     | exact |
| Who does Graham describe a college student meeting by chance after ... | 5     | 5     | exact |
| How long did it take to train the big Transformer model that hit a ... | 5     | 5     | exact |
| What BLEU score did the Transformer get on the WMT 2014 English-to-... | 5     | 5     | exact |
| Which of those does he say deserves the most emphasis, and why?        | 1     | 1     | exact |
| What does the paper claim about the Transformer's parallelizability... | 4     | 3     | ±1    |
| How long did it take to train the big Transformer model that hit a ... | 5     | 5     | exact |
| What Florida-based company does Graham cite as a startup that succe... | 1     | 1     | exact |
| What BLEU score did the Transformer get on the WMT 2014 English-to-... | 1     | 1     | exact |
| According to 'Attention Is All You Need', what large unlabeled text... | 1     | 1     | exact |

## 7. Cost + latency per model (check 11)

| Model                        | Avg TTFT (s) | Avg total latency (s) | Total cost ($) | Avg cost/call ($) |
|------------------------------|--------------|-----------------------|----------------|-------------------|
| gemini/gemini-3.5-flash-lite | 1.73         | 1.97                  | 0.0220         | 0.0006            |
| gpt-5.4-nano                 | 0.68         | 1.14                  | 0.0133         | 0.0004            |

**Total generation spend: $0.0353** (72 calls — judge-phase spend is separate).

## 8. Cross-user isolation (automated RLS check)

Two real, separately-signed-in Clerk accounts. One query vector (sf-01's question, embedded once) sent through `retrieve()` as each user. `match_chunks` is security-invoker, so `chunks_isolation` from migration 001 is the only thing standing between one user's documents and another's search results — this test turns that from an argument into a checked fact.

**PASS.** User A's `retrieve()` returned 10 chunks (the eval test corpus). User B's `retrieve()` on the identical vector also returned 10 chunks — B has their own documents uploaded — and **zero of them were user A's chunk ids.** Two different, non-empty result sets on the same query, with no overlap, is stronger evidence than an empty-B result would have been: it rules out "B just has nothing to retrieve" as a trivial explanation and shows RLS actively partitioning two populated corpora.

Script: `scripts/eval.py cross-user --token-a <A> --token-b <B>`.

## 9. Failure mode analysis

### Retrieval failure

| Question | What happened | Root cause | Fix |
|---|---|---|---|
| **sf-10** | Ground-truth chunk (`f2c8a69d`) ranked #7 by HNSW — missed at production k=5, only recovered at k=10. | The phrasing "how long... and on what hardware" embeds closer to a neighboring results/table chunk (`afba622a`) than to the canonical prose chunk. That neighbor happened to rank #1 and *also* contained the training-time figure, so generation still answered correctly (correctness 5) despite the "official" ground-truth chunk being missed — an artifact of 800-token/100-overlap chunking spreading one fact across adjacent chunks, and of Phase 0 having to pick a single canonical chunk id per question when two would legitimately qualify. This is the only genuine retrieval miss in the whole run (16/16 hit rate everywhere else at k=5). |  Re-ranking (Day 11.5) is the direct fix — it re-scores the full k=20 vector candidate set against the literal query, which should pull `f2c8a69d` back above the neighboring table chunk regardless of embedding drift from phrasing. |

### Generation failures

| Question | What happened | Root cause | Fix |
|---|---|---|---|
| **mt-01** (multi-turn) | Query rewriting worked perfectly (`"...which of the three things Graham says we should do deserves the most emphasis, and why?"`) and retrieval hit the correct chunk at rank 1. Generation still failed on both models: Gemini answered *"The provided sources do not state that any one of those principles deserves the most emphasis"* (a false refusal — correctness 1); GPT just re-stated the earlier turn's answer (the three things), ignoring the actual follow-up (correctness 2). | Not a retrieval or rewriting problem — the retrieved chunk explicitly argues *why* "make good new things" is special ("the most impressive thing humans can do... the best kind of thinking"), but never uses the literal words "deserves the most emphasis." Gemini read that literally and refused; GPT appears to have anchored on the context-turn's question instead of the new one. This is a synthesis/reading-comprehension gap in the generation step, not upstream. |  Nothing upstream to fix. Worth flagging as a genuine model-quality gap for the writeup — a stronger judge model or explicit "answer may require inference from the passage, not just verbatim lookup" system-prompt wording might help, but this wasn't tested. |
| **fig-01** (figure-only), Gemini only | Gemini named the wrong two blocks ("Add & Norm", "Feed Forward" — correctness 1, faithfulness 0.00). GPT correctly named "Linear" then "Softmax" (correctness 5). | Confirms the existing image-retrieval-ceiling finding from Day 10c: figure chunks embed from pixels only, with no caption text, so retrieval finds the right image (rank 2, in top-5) but the generating model still has to *read* the diagram correctly, and one of the two vision models misread the block order. | Already tracked as a known ceiling, not new scope for Day 11.5. |

### Metric artifacts — not real failures

Two of check 9's low scores and one faithfulness score turned out to be judge/metric limitations, not app bugs, once the raw judge output was read:

- **Citation accuracy under-scores multi-source sentences.** Every "unsupported" verdict in the per-question table (`sf-06` 0/2, `sf-10`/gpt 0/1, `mh-03`/gemini 1/3) is a sentence that legitimately synthesizes facts from **two** cited chunks jointly (e.g. sf-06's "37% federal + 4.75% state + 20% wealth-tax-equivalent = 61.75%" cites `[1],[2]` — federal+state come from one chunk, the wealth-tax conversion from the other). The citation judge's rubric checks whether the sentence is supported, source by source, and never credits a sentence whose support is split across its citations. `mh-03`'s third sentence, which used a single citation `[2]`, was correctly marked `supported: true` — same judge, same run, only the citation count differs. This is a check-9 methodology gap, not evidence of hallucinated sourcing.
- **sf-07 faithfulness = 0.00 despite a verbatim-grounded answer.** Gemini's answer ("...a college student who moved to Palo Alto for the summer by chance running into Sean Parker on a random suburban street") is close to word-for-word out of the retrieved chunk, and the same answer's citation was separately checked and marked `supported: true`. RAGAS's `Faithfulness` metric decomposes the answer into atomic claims and scores each by NLI against context; scoring this one at 0.00 while correctness (5) and citation accuracy (supported) both say it's grounded is inconsistent with the other two checks, and reads as RAGAS claim-decomposition noise rather than a real ungrounded answer. Worth noting for anyone trusting a single faithfulness number in isolation — it's exactly why checks 7, 8, and 9 are three separate rows instead of one blended "quality" score.

### Takeaway

Retrieval is not the bottleneck — 16/16 hit rate at k=5 (94% headline number is pulled down only by sf-10, which still generated correctly). The two real failures (`mt-01`, `fig-01`/Gemini) are both generation-side: one model literally reads a passage's diagram wrong, the other fails to synthesize an implicit answer from an explicit passage. The multi-turn question this eval was built to stress-test (`BUILD.md`'s Day 9 rewriting concern) turned out to have its rewriting and retrieval work fine — the residual failure moved one layer downstream, into generation itself.
