# Day 11 eval results

## 1. Extraction fidelity

**TODO — not done yet.** Read the parser's actual output by eye for a 2-column PDF, a table-heavy report, a scanned PDF, and a DOCX with tables. Score each clean / degraded / unusable and note why, save as a JSON list of `{"document": ..., "score": ..., "notes": ...}` at `scripts/eval_cache/extraction_fidelity.json`, then rerun `report`. BUILD.md is explicit that this has to be trusted *before* any retrieval number below it — a chunk that was garbage when written poisons every metric that follows.

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

## 8. Failure mode analysis

*(Fill in by hand: which questions failed, why, and what would fix it. BUILD.md calls this the section interviewers actually read.)*
