"""Day 11: the harness that runs the 18 hand-written Q&A pairs against the real
pipeline and turns the results into `eval_results.md`.

Two things about this app force this script's shape, and neither is optional
(see CLAUDE.md and the Day 11 plan for the full reasoning):

- RLS is the only security boundary, so every Supabase call here needs a real
  Clerk JWT for the test account, pasted by hand — there is no service-role
  bypass anywhere in this codebase, on purpose.
- That token lives ~60 seconds. Everything that touches Supabase (this phase,
  `retrieve`) has to happen in one fast burst; everything after it (paid LLM
  calls, judging) runs later with the token long dead and never needed again.

    apps\\api> .venv\\Scripts\\python.exe ..\\..\\scripts\\eval.py retrieve --token "<jwt>"

Each phase writes its own cache file under `scripts/eval_cache/`, written
incrementally so a slow network or an expired token mid-run loses at most the
question in flight — rerunning with a fresh token picks up where it left off.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# The test corpus is Paul Graham essays and an arXiv paper — both routinely
# contain non-ASCII text ("Erdős"). Windows PowerShell's console defaults to
# cp1252, which can't encode that and crashes on the first `print`. UTF-8 can
# encode anything these files contain, so this is a plain bug fix, not a
# platform-specific special case.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "api"))

import litellm  # noqa: E402
from jose import jwt  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.services import rag  # noqa: E402
from supabase import Client, ClientOptions, create_client  # noqa: E402

SCRIPT_DIR = Path(__file__).resolve().parent
EVAL_QA_PATH = SCRIPT_DIR / "eval_qa.json"
CACHE_DIR = SCRIPT_DIR / "eval_cache"
RETRIEVAL_PATH = CACHE_DIR / "retrieval.json"
CORPUS_PATH = CACHE_DIR / "corpus_chunks.json"
GENERATIONS_PATH = CACHE_DIR / "generations.json"

# rag/no_rag mirrors what a real answer sees at production's default k versus
# what's left if retrieval contributed nothing — the whole point of this phase.
CONDITIONS = ("rag", "no_rag")

# Gemini's free tier caps at 15 requests/minute *per model* — real, hit in
# practice on 2026-08-20 partway through a live run (36 Gemini calls in this
# plan, one call every ~1s with no pacing). GPT-5.4-nano has no such cap here.
# 65s, not the ~29s the error itself suggests: that number is how long was
# left on the window at the *moment* of the error, not a fixed cooldown — a
# minute plus margin is what actually guarantees a fresh window regardless of
# when in it the failure landed.
RATE_LIMIT_MARKERS = ("RateLimitError", "RESOURCE_EXHAUSTED", "429")
RATE_LIMIT_BACKOFF_SECONDS = 65
MAX_RATE_LIMIT_RETRIES = 3


def _is_rate_limit_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}"
    return any(marker in text for marker in RATE_LIMIT_MARKERS)

# `match_chunks` and `match_chunks_exact` both called at 10, once each, per
# question. k=3/5 ablation views are just `[:3]`/`[:5]` slices of this same
# list later — no extra Supabase round trips for them.
MATCH_COUNT = 10

# Below this, don't even start: a burst that begins with too little runway is
# worse than not starting, since a partial run still burns the token. Day 10a
# measured `embed_query` at 7.7s on a cold process, and this run adds a
# `rewrite_query` LLM call for each multi-turn question on top of that — 20s
# is real margin above the slowest plausible single question, not the whole
# burst (the thread pool is what gets 18 questions done inside one token).
MIN_SECONDS_REMAINING = 20

# I/O-bound work (network calls), not CPU-bound, so more workers than cores is
# fine and is the entire point — it's what fits 18 questions inside one
# ~60s-lived token instead of running them one at a time.
MAX_WORKERS = 8

# `eval_qa.json`'s `source_doc` names the essay by its original filename
# (`alien.html`); the test account stores it under the upload's own generic
# name (`Paul Graham-4.txt`). Confirmed by reading the four uploaded .txt
# files directly — the account's own document names give no clue which is
# which. Matched case-insensitively below: the corpus has "paul graham-1.txt"
# lowercase but "Paul Graham-2.txt" capitalized, an inconsistency from upload.
SOURCE_DOC_TO_DOCUMENT_NAME = {
    "hubs.html": "paul graham-1.txt",
    "winc.html": "paul graham-2.txt",
    "do.html": "paul graham-3.txt",
    "alien.html": "paul graham-4.txt",
    "attention_is_all_you_need.pdf": "attention is all you need.pdf",
}

# Common words dropped before scoring a chunk against a question — without
# this, every chunk in English ties on "the", "and", "is" and the ranking
# says nothing.
STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "for", "with",
    "that", "this", "what", "which", "does", "do", "did", "is", "are", "was",
    "were", "how", "why", "who", "it", "its", "as", "by", "at", "from", "says",
    "say", "said", "he", "she", "they", "them", "his", "her", "their", "not",
    "be", "been", "has", "have", "had", "if", "than", "then", "so", "also",
    "into", "about", "over", "under", "out", "up", "down", "more", "most",
    "some", "any", "all", "one", "two", "three", "you", "your", "we", "our",
    "us", "can", "could", "would", "should", "will", "shall", "may", "might",
    "these", "those", "such", "there", "here", "when", "where",
}


def _write_json_atomic(path: Path, data) -> None:
    """Write `data` as JSON to `path` without ever leaving a half-written file.

    Writes to a sibling temp file first, then `os.replace`s it over the real
    path. `os.replace` is atomic on both Windows and POSIX, so a crash or a
    dying token mid-write leaves either the old complete file or the new
    complete file — never a truncated one that `retrieve --resume` would trip
    over on the next run.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


def _is_cached(entry: dict | None) -> bool:
    """Whether a question's retrieval already ran to completion."""
    return bool(entry) and "hnsw10" in entry and "exact10" in entry


def _seconds_remaining(token: str) -> float:
    """How long until this Clerk token expires, from its own `exp` claim.

    Unverified on purpose: this script isn't the security boundary, RLS is —
    a forged token just fails every Supabase call that follows, the same as a
    fresh one that happens to be wrong. All this needs is the number.
    """
    claims = jwt.get_unverified_claims(token)
    return float(claims["exp"]) - time.time()


def _build_supabase_client(token: str) -> Client:
    """A Supabase client acting as the token's owner — copied from
    `app.deps.get_supabase_client`'s body, since that function is wired to
    FastAPI's dependency injection and can't be called standalone.
    """
    settings = get_settings()
    return create_client(
        settings.supabase_url,
        settings.supabase_anon_key,
        options=ClientOptions(
            headers={"Authorization": f"Bearer {token}"},
            auto_refresh_token=False,
            persist_session=False,
        ),
    )


def _retrieve_exact(supabase: Client, query_vector: list[float], k: int) -> list[dict]:
    """The true k nearest chunks, via migration 008's sequential-scan twin of
    `match_chunks`. Not in `rag.py`: this function exists only to grade the
    real pipeline, not to be part of it.
    """
    response = supabase.rpc(
        "match_chunks_exact",
        {"query_embedding": query_vector, "match_count": k},
    ).execute()
    return response.data or []


def _fetch_corpus_chunks(supabase: Client) -> list[dict]:
    """Every chunk in the test account, with its document's name attached.

    `resolve-hints` (Step 3) needs this to show candidate chunks for a human
    to confirm against. Two plain selects rather than a PostgREST embedded
    join (`chunks(..., documents(name))`) — nothing else in this codebase
    relies on that syntax, and a join client-side in a few lines is one less
    thing to get wrong on a script that only runs a handful of times.
    """
    documents = supabase.table("documents").select("id, name").execute().data or []
    chunks = (
        supabase.table("chunks")
        .select("id, document_id, content, chunk_index, chunk_type, page_number")
        .execute()
        .data
        or []
    )

    name_by_document = {doc["id"]: doc["name"] for doc in documents}
    for chunk in chunks:
        chunk["document_name"] = name_by_document.get(chunk["document_id"])

    return chunks

    # In plain English: fetch the documents table (just id and name) and the
    # chunks table (everything resolve-hints needs to show) as two separate,
    # ordinary queries. Then stitch them together in Python — build a
    # dictionary mapping each document's id to its name, and stamp that name
    # onto every chunk that belongs to it.


def _history_for(question: dict) -> list[dict] | None:
    """The one-turn conversation history a multi-turn question builds on, in
    the `{role, content}` shape both `rewrite_query` and `build_messages`
    expect. `None` for every other question type.
    """
    if question["type"] != "multi-turn":
        return None
    turn = question["context_turn"]
    return [
        {"role": "user", "content": turn["question"]},
        {"role": "assistant", "content": turn["answer"]},
    ]


def _retrieve_one(supabase: Client, question: dict) -> dict:
    """Run one question through embed + both retrieval functions + images.

    Multi-turn questions are rewritten first, exactly like `/chat` does — the
    embedding has to be of a standalone question, or "how does that compare"
    embeds to noise.
    """
    history = _history_for(question)
    query_text = rag.rewrite_query(question["question"], history) if history else question["question"]

    query_vector = rag.embed_query(query_text)
    hnsw10 = rag.retrieve(supabase, query_vector, k=MATCH_COUNT)
    exact10 = _retrieve_exact(supabase, query_vector, k=MATCH_COUNT)
    # Matches production's RETRIEVE_K=5 — the images a real answer at the
    # default k would actually see.
    images = rag.load_images(supabase, hnsw10[:5])

    return {
        "rewritten_query": query_text,
        "hnsw10": hnsw10,
        "exact10": exact10,
        "images": images,
    }


def cmd_retrieve(token: str) -> int:
    remaining = _seconds_remaining(token)
    if remaining < MIN_SECONDS_REMAINING:
        print(
            f"Only {remaining:.0f}s left on this token (need {MIN_SECONDS_REMAINING}s "
            "minimum to start). Paste a fresh one."
        )
        return 1

    print(f"Token has {remaining:.0f}s left — starting the burst.")
    supabase = _build_supabase_client(token)
    started = time.monotonic()

    corpus = _fetch_corpus_chunks(supabase)
    _write_json_atomic(CORPUS_PATH, corpus)
    print(f"Wrote {len(corpus)} chunk(s) to {CORPUS_PATH.name}.")

    questions = json.loads(EVAL_QA_PATH.read_text(encoding="utf-8"))
    cache: dict = json.loads(RETRIEVAL_PATH.read_text(encoding="utf-8")) if RETRIEVAL_PATH.exists() else {}
    pending = [q for q in questions if not _is_cached(cache.get(q["id"]))]

    if not pending:
        print("All 18 questions already cached — nothing to do.")
        return 0

    print(f"Retrieving {len(pending)} question(s) across {MAX_WORKERS} workers...")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_retrieve_one, supabase, q): q for q in pending}
        for future in as_completed(futures):
            question = futures[future]
            try:
                cache[question["id"]] = future.result()
            except Exception as exc:  # noqa: BLE001 — logged and skipped, not fatal
                print(f"  {question['id']}: FAILED ({type(exc).__name__}: {exc})")
                continue
            _write_json_atomic(RETRIEVAL_PATH, cache)
            print(f"  {question['id']}: done")

    # In plain English, the block above: hand all pending questions to the
    # thread pool at once, then process them in whatever order they actually
    # finish (`as_completed`) rather than the order they were submitted —
    # that's what lets a slow one not hold up writing the fast ones to disk.
    # Each success is saved to the cache file immediately; a failure is
    # printed and left out of the cache, so a rerun retries only that one.

    elapsed = time.monotonic() - started
    missing = [q["id"] for q in questions if not _is_cached(cache.get(q["id"]))]
    print(f"\nDone in {elapsed:.1f}s. {len(questions) - len(missing)}/{len(questions)} cached.")
    if missing:
        print(f"Still missing: {', '.join(missing)} — rerun with a fresh token to fill these in.")

    return 0


def _messages_for(question: dict, condition: str, retrieval_entry: dict) -> tuple[list[dict], list[str]]:
    """The exact messages `stream_answer` would receive for one question under
    one condition, plus the chunk ids used (empty for `no_rag`).

    `rag` always takes the top `RETRIEVE_K` of Phase 1's cached hnsw10 —
    matching production's own default k, not the k=10 the retrieval cache
    happens to hold. The original (unrewritten) question goes to the model,
    with history to explain it — `rewrite_query`'s output is for the
    *embedding* only and is never shown to an answering model, in eval or in
    `/chat`.
    """
    history = _history_for(question)
    if condition == "rag":
        chunks = retrieval_entry["hnsw10"][: rag.RETRIEVE_K]
        images = retrieval_entry["images"]
    else:
        chunks, images = [], {}

    messages = rag.build_messages(question["question"], chunks, images, history)
    return messages, [chunk["id"] for chunk in chunks]


def _call_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float | None:
    """Estimated USD cost of one call, or `None` if litellm has no pricing
    entry for this model. Never fatal: a call still happened and is still
    worth recording even if we can't price it.
    """
    try:
        input_cost, output_cost = litellm.cost_per_token(
            model=model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
        )
        return input_cost + output_cost
    except Exception:  # noqa: BLE001 — pricing is a nice-to-have, not a blocker
        return None


def _generate_one(question: dict, model: str, condition: str, retrieval_entry: dict) -> dict:
    """One real call: build the prompt, stream the answer, time it, price it."""
    messages, chunk_ids = _messages_for(question, condition, retrieval_entry)
    input_tokens = litellm.token_counter(model=model, messages=messages)

    started = time.monotonic()
    first_token_at: float | None = None
    parts: list[str] = []
    for token in rag.stream_answer(model, messages):
        if first_token_at is None:
            first_token_at = time.monotonic()
        parts.append(token)
    finished = time.monotonic()

    answer = "".join(parts)
    output_tokens = litellm.token_counter(model=model, text=answer) if answer else 0

    return {
        "answer": answer,
        "chunk_ids": chunk_ids,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": _call_cost(model, input_tokens, output_tokens),
        "time_to_first_token": (first_token_at - started) if first_token_at else None,
        "total_latency": finished - started,
    }


def cmd_generate() -> int:
    """Phase 2a: every question answered by every model, with and without
    retrieval. No Supabase calls at all — everything it needs is already on
    disk from Phase 1, which is the entire reason this phase can take its
    time instead of racing a dying token.
    """
    if not RETRIEVAL_PATH.exists():
        print(f"{RETRIEVAL_PATH.name} not found. Run `retrieve` first.")
        return 1

    questions = json.loads(EVAL_QA_PATH.read_text(encoding="utf-8"))
    retrieval = json.loads(RETRIEVAL_PATH.read_text(encoding="utf-8"))
    cache: dict = json.loads(GENERATIONS_PATH.read_text(encoding="utf-8")) if GENERATIONS_PATH.exists() else {}

    for model in rag.SUPPORTED_MODELS:
        rag.api_key_for(model)  # fail fast on a missing key, before spending anything

    plan = [
        (question, model, condition)
        for question in questions
        for model in rag.SUPPORTED_MODELS
        for condition in CONDITIONS
    ]
    pending = [
        (q, model, condition)
        for q, model, condition in plan
        if f"{q['id']}:{model}:{condition}" not in cache
    ]

    if not pending:
        print(f"All {len(plan)} calls already cached — nothing to do.")
        return 0

    estimated_cost = 0.0
    unpriced = 0
    for question, model, condition in pending:
        messages, _ = _messages_for(question, condition, retrieval[question["id"]])
        input_tokens = litellm.token_counter(model=model, messages=messages)
        # Worst case, not typical: MAX_ANSWER_TOKENS as the assumed output
        # length is the safe direction to be wrong in for a number whose whole
        # job is to be checked before real money moves.
        cost = _call_cost(model, input_tokens, rag.MAX_ANSWER_TOKENS)
        if cost is None:
            unpriced += 1
        else:
            estimated_cost += cost

    print(f"{len(pending)} call(s) pending ({len(cache)}/{len(plan)} already cached).")
    print(f"Estimated cost (worst case): ${estimated_cost:.2f}", end="")
    print(f", plus {unpriced} call(s) litellm has no pricing for." if unpriced else ".")

    if input('Type "yes" to spend real money and proceed, anything else to cancel: ').strip().lower() != "yes":
        print("Cancelled — no calls made.")
        return 0

    started = time.monotonic()
    actual_cost = 0.0
    for question, model, condition in pending:
        key = f"{question['id']}:{model}:{condition}"
        result = None
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            try:
                result = _generate_one(question, model, condition, retrieval[question["id"]])
                break
            except Exception as exc:  # noqa: BLE001 — logged and skipped, not fatal
                if _is_rate_limit_error(exc) and attempt < MAX_RATE_LIMIT_RETRIES:
                    print(
                        f"  {key}: rate-limited, waiting {RATE_LIMIT_BACKOFF_SECONDS}s "
                        f"(retry {attempt + 1}/{MAX_RATE_LIMIT_RETRIES})..."
                    )
                    time.sleep(RATE_LIMIT_BACKOFF_SECONDS)
                    continue
                print(f"  {key}: FAILED ({type(exc).__name__}: {exc})")

        if result is None:
            continue
        cache[key] = result
        _write_json_atomic(GENERATIONS_PATH, cache)
        actual_cost += result["cost_usd"] or 0.0
        print(f"  {key}: done (${result['cost_usd'] or 0:.4f}, {result['total_latency']:.1f}s)")

    elapsed = time.monotonic() - started
    missing = [
        f"{q['id']}:{model}:{condition}"
        for q, model, condition in plan
        if f"{q['id']}:{model}:{condition}" not in cache
    ]
    print(f"\nDone in {elapsed:.0f}s. Spent ${actual_cost:.4f}. {len(plan) - len(missing)}/{len(plan)} cached.")
    if missing:
        print(f"Still missing: {', '.join(missing)} — rerun `generate` to retry just these.")

    return 0

    # In plain English, this command: first works out every (question, model,
    # with/without-retrieval) combination there should be — 18 x 2 x 2 = 72 —
    # and drops any that are already saved from a previous run. Then, before
    # spending a cent, it adds up what the *worst case* would cost (assuming
    # every answer runs to the maximum allowed length) and shows you that
    # number, refusing to go further until you type "yes". Only after that
    # does it actually call the models, one at a time, saving each answer to
    # disk the moment it arrives — so if it's interrupted partway, rerunning
    # only pays for and redoes the ones that didn't finish.


def _keywords(text: str) -> set[str]:
    """The significant words in `text`: lowercased, stopwords and short words
    dropped. What's left is usually the words specific enough to a passage to
    be worth matching on — "Wufoo", "Tampa" — rather than words every passage
    shares.
    """
    tokens = re.findall(r"[a-z0-9']+", (text or "").lower())
    return {token for token in tokens if len(token) > 2 and token not in STOPWORDS}


def _score_chunk(chunk: dict, keywords: set[str]) -> int:
    """How many of `keywords` actually appear in this chunk's text."""
    return len(keywords & _keywords(chunk.get("content")))


def cmd_resolve_hints() -> int:
    """Phase 0: a human confirms which real chunk backs each question's answer.

    Interactive by design (input() per question) — this is meant to be run
    directly in your own terminal, not through me, same as `retrieve`.
    """
    if not CORPUS_PATH.exists():
        print(f"{CORPUS_PATH.name} not found. Run `retrieve` first.")
        return 1

    corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    questions = json.loads(EVAL_QA_PATH.read_text(encoding="utf-8"))

    todo = [q for q in questions if q["type"] != "adversarial" and not q.get("ground_truth_chunk_id")]
    if not todo:
        print("Every non-adversarial question already has a ground_truth_chunk_id.")
        return 0

    print(f"{len(todo)} question(s) to resolve. The 2 adversarial questions are skipped by design.\n")

    resolved_this_run = 0
    for position, question in enumerate(todo, start=1):
        document_name = SOURCE_DOC_TO_DOCUMENT_NAME[question["source_doc"]]
        scoped = [c for c in corpus if (c["document_name"] or "").lower() == document_name]
        keywords = _keywords(
            f"{question['question']} {question['ground_truth_answer']} {question['ground_truth_chunk_hint']}"
        )

        while True:
            if question["type"] == "figure-only":
                # An image chunk's stored "content" is just a label
                # ("[Image from page 3]"), so it always scores 0 against
                # real question keywords and would never reach a plain
                # top-5 — the one case this eval set has of the right
                # answer being unrankable by text at all. Image chunks go
                # first, ordered by page since that's how a human would
                # scan the source PDF; a few top-scoring text chunks ride
                # along in case the real target is a caption, not a figure.
                images = sorted(
                    (c for c in scoped if c["chunk_type"] == "image"),
                    key=lambda c: c.get("page_number") or 0,
                )
                texts = sorted(
                    (c for c in scoped if c["chunk_type"] != "image"),
                    key=lambda c: _score_chunk(c, keywords),
                    reverse=True,
                )[:3]
                candidates = images + texts
            else:
                candidates = sorted(scoped, key=lambda c: _score_chunk(c, keywords), reverse=True)[:5]

            print(f"--- [{position}/{len(todo)}] {question['id']} ({question['type']}) — {question['source_doc']} ---")
            print(f"Q: {question['question']}")
            print(f"A: {question['ground_truth_answer']}")
            print(f"Hint: {question['ground_truth_chunk_hint']}\n")
            for i, chunk in enumerate(candidates, start=1):
                preview = (chunk.get("content") or "").replace("\n", " ")[:160]
                print(
                    f"  {i}) [score {_score_chunk(chunk, keywords)}] "
                    f"chunk #{chunk['chunk_index']} ({chunk['chunk_type']}, page {chunk.get('page_number')})"
                )
                print(f"     {preview}")

            answer = input(
                '\nCorrect chunk number(s), e.g. "1" or "2,4" for multi-hop, '
                '"search <word>" to re-rank, or "skip": '
            ).strip()

            if not answer or answer.lower() == "skip":
                print("Skipped — will show again next run.\n")
                break

            if answer.lower().startswith("search "):
                keywords = _keywords(answer[len("search ") :])
                print()
                continue

            try:
                indices = [int(token) for token in answer.split(",")]
                selected = [candidates[i - 1]["id"] for i in indices]
            except (ValueError, IndexError):
                print(f"Could not read {answer!r} as candidate number(s). Try again.\n")
                continue

            question["ground_truth_chunk_id"] = selected
            _write_json_atomic(EVAL_QA_PATH, questions)
            resolved_this_run += 1
            print(f"Saved. ground_truth_chunk_id = {selected}\n")
            break

    # In plain English, the loop above: for each question needing a ground
    # truth, show its 5 best-guess chunks (ranked by how many of the
    # question's distinctive words appear in each one) and ask you to type
    # the right one's number. Typing "search wealth tax" throws away the
    # automatic guess and re-ranks by whatever words you give it instead — an
    # escape hatch for when none of the top 5 look right. Every confirmed
    # answer is saved to eval_qa.json immediately, so closing the terminal
    # partway through only costs the question you were on, not the ones
    # already done.

    remaining = [q["id"] for q in questions if q["type"] != "adversarial" and not q.get("ground_truth_chunk_id")]
    print(f"Resolved {resolved_this_run} this run. {len(remaining)} still unresolved: {remaining or 'none'}.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    retrieve_parser = subparsers.add_parser("retrieve", help="Phase 1: embed + retrieve all 18 questions.")
    retrieve_parser.add_argument("--token", required=True, help="A fresh Clerk session JWT.")

    subparsers.add_parser("resolve-hints", help="Phase 0: confirm ground-truth chunks by hand.")

    subparsers.add_parser("generate", help="Phase 2a: answer every question, every model, rag vs no-rag.")

    args = parser.parse_args()

    if args.command == "retrieve":
        return cmd_retrieve(args.token)
    if args.command == "resolve-hints":
        return cmd_resolve_hints()
    if args.command == "generate":
        return cmd_generate()

    return 1  # pragma: no cover — argparse's `required=True` already rejects this


# ---------------------------------------------------------------------------
# Self-check
# ---------------------------------------------------------------------------

if __name__ == "__main__" and len(sys.argv) == 1:
    # Only the pure, network-free logic — the actual retrieval needs a live
    # token and a real database, which is what `retrieve` itself is for.
    #   apps\api> .venv\Scripts\python.exe ..\..\scripts\eval.py
    import tempfile

    assert not _is_cached(None), "a missing entry read as cached"
    assert not _is_cached({"hnsw10": []}), "a half-written entry (no exact10) read as cached"
    assert _is_cached({"hnsw10": [], "exact10": []}), "a complete entry (even with 0 hits) read as not cached"

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "nested" / "out.json"
        _write_json_atomic(target, {"a": 1})
        assert json.loads(target.read_text()) == {"a": 1}, "atomic write did not round-trip"
        assert not target.with_suffix(".json.tmp").exists(), "temp file was left behind"
        _write_json_atomic(target, {"a": 2})
        assert json.loads(target.read_text()) == {"a": 2}, "atomic write did not overwrite"

    fake_token = jwt.encode({"exp": time.time() + 100}, "not-a-real-secret", algorithm="HS256")
    remaining = _seconds_remaining(fake_token)
    assert 95 < remaining <= 100, f"expected ~100s remaining, got {remaining:.1f}"

    kws = _keywords("The Wufoo company is based in Tampa, Florida.")
    assert "wufoo" in kws and "tampa" in kws, "significant words were not extracted"
    assert "the" not in kws and "is" not in kws, "stopwords leaked through"

    relevant = {"id": "a", "content": "Wufoo is a Tampa-based startup."}
    unrelated = {"id": "b", "content": "Nothing relevant here."}
    assert _score_chunk(relevant, kws) > _score_chunk(unrelated, kws), (
        "keyword overlap did not rank the relevant chunk higher"
    )

    print("OK — cache/atomic-write/token-expiry/keyword-scoring logic checked without a network call.")
elif __name__ == "__main__":
    raise SystemExit(main())
