"""Question in, grounded answer out.

The mirror of `ingestion.py`: plain functions, no FastAPI, no routing. The
endpoint in `routers/chat.py` orchestrates these, and Day 11's eval harness
imports them directly — which is the reason they live here rather than inside
the request handler. An ablation that has to boot a web server to measure
retrieval is an ablation nobody runs twice.

The load-bearing property in this file is that `embed_query` asks Cohere for a
*query* vector, not a document vector. Getting that wrong raises nothing and
returns worse answers forever.
"""

from __future__ import annotations

import base64
import logging

import litellm
from langsmith import traceable

from app.config import get_settings
from app.services import ingestion, tracing

logger = logging.getLogger(__name__)

# The allowlist, and where each model's key lives on `Settings`. A dict rather
# than two parallel constants because the mapping is the thing that matters: a
# model nobody has a key for is not a supported model.
#
# Verified vision-capable 2026-08-07 — not optional. Day 6b stores figures as
# image chunks, so a text-only model would receive the label "[Image from page
# 4]" and confidently answer from nothing.
#
# The default was `gemini-2.5-flash-lite` until 2026-08-11, when the first real
# call returned 404: "no longer available to new users". Its published shutdown
# date is still months away, and Google's own catalogue endpoint still lists it —
# access had simply been closed to keys minted after some earlier cutoff. Being
# listed is not being callable, and only an actual request tells the difference.
#
# `gemini-3.5-flash-lite` is $0.30/$2.50 against the old $0.10/$0.40. Roughly
# $0.002 a question at k=5, which is the price of a default that will still
# answer next month.
SUPPORTED_MODELS = {
    "gemini/gemini-3.5-flash-lite": "gemini_api_key",
    "gpt-5.4-nano": "openai_api_key",
}

DEFAULT_MODEL = "gemini/gemini-3.5-flash-lite"

# How many chunks a question retrieves. Day 11 sweeps 3/5/10 by passing `k`
# directly, which is also why this is not a field on the API request: a caller
# choosing k is a caller choosing how much of my money to spend per question.
RETRIEVE_K = 5

# Ceiling on one answer. Not a quality setting — a brake. Without it a model
# that decides to enumerate a whole document bills the full output window.
MAX_ANSWER_TOKENS = 1000

# Wall-clock ceilings on a provider call, in seconds. Passing no timeout does
# not mean "fast" — it means inheriting `litellm.request_timeout`, which is
# 6000.0, i.e. a hundred minutes (checked, not assumed, 2026-08-13). A
# provider that accepts the connection and then goes quiet is the case these
# bound; until now the only thing that noticed was the browser's idle guard,
# 20s after the last token, which cannot help the two helpers at all because
# neither is streamed and so neither has a last token to count from.
#
# Two numbers rather than three: `rewrite_query` and `generate_title` are the
# same shape of call — one short sentence from DEFAULT_MODEL, with a caller
# already holding a fallback — so both would rather give up early than make the
# user wait for something optional. The answer itself has no fallback and is the
# thing the user is actually waiting for, so it gets the patient number.
#
# ponytail: this bounds a provider that never responds or stalls between chunks,
# not the total length of a healthy stream. A model that keeps steadily emitting
# is bounded by MAX_ANSWER_TOKENS instead. Add a total-duration cap only if a
# real answer ever legitimately runs that long.
HELPER_TIMEOUT = 10
ANSWER_TIMEOUT = 60

# Enough of a chunk to recognise it in a citation, not enough to bloat the
# `messages.sources` JSON that Day 8 renders on every message.
PREVIEW_CHARS = 300

# A title is six words. Twenty tokens is roughly triple that, which is slack for
# a model that opens with "Sure, here you go" rather than room to write an essay.
TITLE_TOKENS = 20

# How much of a conversation the model gets to see. Three exchanges is what
# BUILD.md asks for; "turn" means one question *and* its answer, so this is six
# database rows.
HISTORY_TURNS = 3

# Per message, not per history. `MAX_ANSWER_TOKENS = 1000` lets a single answer
# reach roughly 4000 characters, so three unbounded turns could quietly add
# thousands of tokens to every follow-up. CLAUDE.md makes spend a design
# constraint, and a bounded worst case is one slice.
HISTORY_CHARS = 1000

# A standalone question is one sentence. This is a brake on a model that decides
# to explain its rewrite rather than a target length.
REWRITE_TOKENS = 60

# The bound `ConversationUpdate` puts on a human rename, applied to the model's
# output for the same reason.
TITLE_MAX_CHARS = 200

TITLE_PROMPT = """Write a title of at most 4 words for the question below.

Rules:
- Reply with the title and nothing else. No quotes, no preamble, no full stop.
- Name the subject, not the act of asking: "Ionospheric delay correction", never
  "Question about the document".
"""

REWRITE_PROMPT = """Rewrite the user's latest question so it can be understood on \
its own, without the conversation.

Rules:
- Use the conversation to fill in what the question leaves out: pronouns, "it", \
"the second one", or a subject that is simply missing.
- If the question already stands on its own, reply with it word for word \
unchanged. Do not improve it, and do not add the topic of the conversation to a \
question that did not mention it.
- The user is allowed to change the subject. Never carry the earlier topic into \
a question that has moved on from it.
- Reply with the rewritten question and nothing else. No preamble, no \
explanation, no quotes.
- Do not answer the question. Only rewrite it.
"""

SYSTEM_PROMPT = """You answer questions using only the numbered sources below.

Rules:
- Ground every claim in the sources. Do not use outside knowledge.
- Cite the sources you used inline, like [1] or [2][3].
- Some sources are images. Read them as carefully as the text.
- Earlier messages in this conversation may cite numbers of their own. Ignore \
them: [1], [2] and so on always mean the numbered sources in this turn.
- If the sources do not contain the answer, say so plainly and stop. Do not \
guess, and do not pad the answer with what the sources *do* say unless it is \
genuinely relevant.
"""


# ---------------------------------------------------------------------------
# Retrieve
# ---------------------------------------------------------------------------


@traceable(run_type="embedding")
def embed_query(question: str) -> list[float]:
    """Turn the user's question into a vector in the chunks' space.

    `input_type="search_query"` is the entire point of this wrapper. Cohere
    embeds a stored passage and a search query differently, and ingestion used
    the default `search_document`. Passing the same default here would degrade
    every search *silently* — no error, no log line, just answers that are a bit
    worse than they should be, forever.
    """
    return ingestion.embed([question], input_type="search_query")[0]

    # In plain English: a vector is a long list of numbers that stands for the
    # meaning of a piece of text — two texts about the same thing get similar
    # lists. `embed` is built to handle many texts at once, so we wrap the one
    # question in a list to hand it over, and `[0]` takes the single result back
    # out of the list it hands back.


@traceable(
    run_type="retriever",
    process_inputs=tracing.clean_inputs,
    # The chunks carry `image_path`, which begins with the owner's Clerk id. This
    # span is the most-read one in the trace and the likeliest to be shared
    # publicly, so its output goes through the same redaction as the image
    # payloads do.
    process_outputs=tracing.redact,
)
def retrieve(supabase, query_vector: list[float], k: int = RETRIEVE_K) -> list[dict]:
    """The k most similar chunks, nearest first.

    Takes the caller's Supabase client, and takes no user id. That is not an
    oversight: `match_chunks` is deliberately *security invoker*, so the
    `chunks_isolation` and `documents_isolation` policies from 001 apply inside
    the function body and scope the search to whoever holds this token. Passing
    a user id and filtering here would put the security boundary in Python,
    which CLAUDE.md rules out.

    Each row carries `similarity` (1 - cosine distance) alongside the content.
    Cohere embed-v4's scores are compressed — a near-verbatim quote measured
    0.68, not 0.95 — so do not read these as percentages. Day 11.5 sets an
    abstention threshold from real numbers.
    """
    response = supabase.rpc(
        "match_chunks",
        {"query_embedding": query_vector, "match_count": k},
    ).execute()

    return response.data or []

    # In plain English: `rpc` means "run a function that lives inside the
    # database" — here it is `match_chunks` from migration 005, which compares
    # our question's vector against every stored chunk's vector and hands back
    # the closest `k` of them. `.execute()` is what actually sends the request.
    # `response.data or []` means: use the rows if there are any, otherwise an
    # empty list, so the caller never has to check for `None`.


@traceable(
    run_type="tool",
    process_inputs=tracing.clean_inputs,
    # The return value is `{path: data URI}` — the one output in the pipeline
    # that is megabytes of base64 rather than text.
    process_outputs=tracing.redact,
)
def load_images(supabase, chunks: list[dict]) -> dict[str, str]:
    """Fetch the JPEG behind every image chunk, as `{image_path: data URI}`.

    Storage holds the pictures; the chunks table holds only their paths. Without
    this step an image chunk contributes its label and nothing else, and Day 6b
    may as well not have happened.

    A download that fails is logged and skipped rather than raised. One
    unreachable figure should cost that figure, not the whole answer — the text
    chunks retrieved alongside it are still worth sending. The model is told
    which sources are images, so a missing one degrades to a source it can see
    the description of but not the content, which is honest.
    """
    images: dict[str, str] = {}

    for chunk in chunks:
        path = chunk.get("image_path")
        if chunk.get("chunk_type") != "image" or not path or path in images:
            continue

        try:
            jpeg = ingestion.download(supabase, path)
        except Exception as exc:
            # The exception's class and message, but never the response body: a
            # storage client can echo request headers, and those must not reach a
            # log. Storage errors carry their own short message ("Object not
            # found", an RLS refusal), which is the part worth having.
            #
            # This used to log neither. On 2026-08-14 all four images behind one
            # answer failed and the log said only "answering without it" four
            # times — enough to know something broke, not enough to know what,
            # while the answer went out looking perfectly normal.
            logger.warning(
                "Could not load image chunk %s; answering without it (%s: %s)",
                path,
                type(exc).__name__,
                exc,
            )
            continue

        images[path] = f"data:image/jpeg;base64,{base64.b64encode(jpeg).decode()}"

    return images

    # In plain English, the loop above: go through the retrieved chunks one at a
    # time and ignore any that are not pictures, have no stored path, or that we
    # already fetched — that last check stops us downloading the same figure
    # twice if it was retrieved twice.
    #
    # For the ones left, pull the JPEG out of Supabase Storage. If that download
    # fails we write a line to the log and move to the next one instead of
    # crashing, so a single missing figure costs that figure and not the whole
    # answer.
    #
    # The last line turns the raw picture into a "data URI" — the image rewritten
    # as one very long line of plain text, which is the only form you can put
    # inside a JSON message to a model. `base64` is the encoding that does that
    # rewriting.


@traceable(run_type="retriever", process_inputs=tracing.clean_inputs)
def load_history(supabase, conversation_id, turns: int = HISTORY_TURNS) -> list[dict]:
    """The last few messages of a conversation, oldest first.

    Takes the caller's client and no user id, exactly like `retrieve` — the
    `messages_isolation` policy from `001_init.sql` is the boundary, per
    CLAUDE.md. An id belonging to somebody else returns an empty list here, which
    is the same thing a conversation with no messages returns, which is correct:
    both mean "there is no history to use".

    Only `role` and `content` are selected. The `sources` column is deliberately
    left behind — re-sending the chunks earlier turns retrieved is the version of
    this feature that costs real money, and those chunks are already baked into
    the answers as prose (BUILD.md line 387).

    Raises on failure. The router decides that a memory failure should degrade to
    a history-less answer rather than an error page, and that decision belongs
    there, not here.
    """
    response = (
        supabase.table("messages")
        .select("role, content")
        .eq("conversation_id", str(conversation_id))
        # Newest first so `limit` takes the *recent* end of the conversation, and
        # then `role` breaks the tie: both rows of one exchange share a timestamp
        # (see the note in `conversations.py`). Ascending role puts 'assistant'
        # above 'user' here — which is what we want, because the whole list is
        # reversed below and the pair has to land user-then-assistant.
        .order("created_at", desc=True)
        .order("role")
        .limit(turns * 2)
        .execute()
    )

    rows = list(reversed(response.data or []))

    return [
        {"role": row["role"], "content": (row["content"] or "")[:HISTORY_CHARS]}
        for row in rows
    ]

    # In plain English: ask the database for this conversation's messages newest
    # first, keep only the last six rows (three questions and three answers), then
    # flip them back into reading order — newest-first was only ever a trick to
    # make `limit` grab the recent end instead of the beginning.
    #
    # The final list rebuilds each row as just a role and its text, trimmed to
    # 1000 characters so one rambling answer cannot inflate the cost of every
    # question that follows it.


@traceable(run_type="llm")
def rewrite_query(question: str, history: list[dict]) -> str:
    """A follow-up question, rewritten to stand on its own.

    This exists for the *vector*, not for the answer. "give count" embeds to
    nothing useful because the words carry no subject, so retrieval returns noise
    however good the prompt downstream is. Rewriting happens before `embed_query`
    or it may as well not happen at all.

    The result is never shown to the user, never saved, and never sent to the
    answering model — that one keeps the original question, with history in the
    prompt to explain it.

    Always `DEFAULT_MODEL`, like `generate_title`: a search helper has no business
    billing the expensive model the caller picked for their answer.

    Raises rather than returning a fallback, again like `generate_title`. The
    caller's fallback — the original question — is perfectly good, and inventing
    it down here would hide the failure from the log.
    """
    # ponytail: verified 2026-08-12 against the real "give count" exchange — it
    # resolves the missing subject, leaves standalone questions alone, and does
    # not drag the old topic into a question that changed the subject. What it
    # gets wrong is ordinals: "the third one" resolved to the second item in a
    # list. Retrieval is fuzzy enough to survive that (neighbouring items are
    # usually in neighbouring chunks) and the answering model still sees the
    # original question plus the history. Revisit with a stronger model here if
    # Day 11's eval shows ordinal follow-ups failing.
    api_key = api_key_for(DEFAULT_MODEL)

    conversation = "\n".join(f"{turn['role']}: {turn['content']}" for turn in history)

    response = litellm.completion(
        model=DEFAULT_MODEL,
        messages=[
            {"role": "system", "content": REWRITE_PROMPT},
            {
                "role": "user",
                "content": f"Conversation so far:\n{conversation}\n\nLatest question: {question}",
            },
        ],
        api_key=api_key,
        max_tokens=REWRITE_TOKENS,
        timeout=HELPER_TIMEOUT,
    )

    rewritten = (response.choices[0].message.content or "").strip().strip('"')

    if not rewritten:
        raise ValueError("The model returned an empty rewrite.")

    return rewritten

    # In plain English: the history is flattened into one block of plain text
    # ("user: ...", "assistant: ...") and handed over as ordinary input rather
    # than as a real chat exchange. That is on purpose — given a real exchange a
    # model tends to *continue* the conversation, and we want it to look at the
    # conversation from outside and edit one sentence.


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


@traceable(
    run_type="prompt",
    # Both sides carry images: `images` comes in as data URIs and the assembled
    # prompt has them inlined in its content parts.
    process_inputs=tracing.clean_inputs,
    process_outputs=tracing.redact,
)
def build_messages(
    question: str,
    chunks: list[dict],
    images: dict[str, str],
    history: list[dict] | None = None,
) -> list[dict]:
    """Assemble the chat messages: system rules, history, sources, question.

    Sources are numbered from 1 in retrieval order, and that number is what the
    model cites. It is deliberately *not* `chunk_index` — that counts position
    within a document, so two chunks from two documents can both be index 12 and
    a citation would be ambiguous.

    An image chunk contributes two parts: a text header naming it, then the
    picture itself. The header is what lets the model write "[3]" about
    something it saw rather than read.

    The question goes last. Models attend most reliably to the end of the
    prompt, and putting it after the sources also keeps the long, stable part of
    the message first — which is the shape prompt caching would want if we add
    it later.

    `history` goes in as real user/assistant messages rather than as a block of
    text inside the system prompt: it is the shape a chat model is trained on,
    and it is less code than flattening. It sits *above* the sources so the last
    thing the model reads is still this turn's evidence and this turn's question.

    Optional, so the eval harness and the self-check below keep calling this with
    three arguments and get byte-identical output to before Day 9b.
    """
    parts: list[dict] = []

    for number, chunk in enumerate(chunks, start=1):
        label = f"[{number}] {chunk['document_name']}"
        if chunk.get("page_number"):
            label += f", page {chunk['page_number']}"

        data_uri = images.get(chunk.get("image_path") or "")

        if data_uri:
            parts.append({"type": "text", "text": f"{label} (image):"})
            parts.append({"type": "image_url", "image_url": {"url": data_uri}})
        else:
            parts.append({"type": "text", "text": f"{label}:\n{chunk['content']}"})

    # In plain English, the loop above: build the message the model will read,
    # one piece ("part") at a time. `enumerate(chunks, start=1)` walks the chunks
    # while counting 1, 2, 3... — that count becomes the [1] [2] [3] the model
    # cites.
    #
    # Each chunk first gets a label naming its document and page. Then it splits
    # two ways: if we managed to load a picture for this chunk, add a short text
    # part saying "here comes an image" followed by the picture itself; otherwise
    # add one text part holding the chunk's words. The model sees a numbered list
    # either way, which is why it can cite a figure and a paragraph the same way.

    parts.append({"type": "text", "text": f"\nQuestion: {question}"})

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        *(history or []),
        {"role": "user", "content": parts},
    ]

    # In plain English: a chat model is always sent a list of messages, each
    # tagged with who is speaking. "system" is the standing instructions — the
    # rules about citing and not guessing. "user" is this turn's actual input,
    # which here is all the sources plus the question stuck on the end.
    #
    # The middle line is the Day 9b addition. `*` means "unpack this list into
    # this one" — so three past messages become three entries here rather than
    # one entry holding a list. `history or []` covers the no-history case: an
    # empty list unpacks to nothing at all, which is why an old three-argument
    # call still produces exactly the two messages it always did.


def to_sources(chunks: list[dict]) -> list[dict]:
    """The citation records saved on the assistant message and rendered by Day 8.

    Built here rather than in the router because the numbering has to match
    `build_messages` exactly — a "[2]" in the answer text and the second entry
    in this list are a promise to the reader. Keeping both in one file is what
    makes that promise checkable.
    """
    return [
        {
            "number": number,
            # The address of the picture, for an image chunk. `image_path` below
            # is a Storage path the browser holds no credential for, so it can
            # name the file but never fetch it; this id is what
            # `GET /documents/{document_id}/images/{chunk_id}` accepts, and the
            # database decides whether to answer.
            #
            # Absent from every `sources` blob written before this change, so
            # the UI must treat it as optional — an older answer keeps its
            # citations and simply shows no picture.
            "chunk_id": chunk["id"],
            "document_id": chunk["document_id"],
            "document_name": chunk["document_name"],
            "chunk_index": chunk["chunk_index"],
            "page_number": chunk.get("page_number"),
            "chunk_type": chunk.get("chunk_type", "text"),
            "image_path": chunk.get("image_path"),
            "content_preview": chunk["content"][:PREVIEW_CHARS],
            "similarity": chunk.get("similarity"),
        }
        for number, chunk in enumerate(chunks, start=1)
    ]

    # In plain English: this is a "list comprehension" — a compact way of writing
    # a loop that builds a list. Read it bottom-up: for every chunk (counting
    # from 1), make one record holding the facts a citation needs. `[:300]` on
    # the content takes only the first 300 characters, since this whole list is
    # stored in the database on every message and the full text is already
    # stored once in `chunks`.


# ---------------------------------------------------------------------------
# Generate
# ---------------------------------------------------------------------------


def api_key_for(model: str) -> str:
    """The configured key for this model, or a ValueError naming the problem.

    Both failures are the same shape to the caller — "this model can't answer
    right now" — so they raise the same type and the router maps it to one
    status code. Neither message contains a key value; see CLAUDE.md.

    Public, not `_private`, because the router calls it during pre-flight: it
    needs the failure *before* the response starts, while an HTTP status code is
    still possible. `stream_answer` calls it again, which costs a dictionary
    lookup and keeps the function safe to call on its own.
    """
    attribute = SUPPORTED_MODELS.get(model)
    if attribute is None:
        raise ValueError(f"{model} is not a supported model.")

    key = getattr(get_settings(), attribute)
    if not key:
        raise ValueError(f"{model} is not configured on this server. Try another model.")

    return key

    # In plain English: `SUPPORTED_MODELS` maps a model name to the *name of the
    # setting* holding its key — "gemini/gemini-2.5-flash-lite" points at the
    # text "gemini_api_key". `getattr` then looks up a setting by its name given
    # as text, which is what lets one line serve every model instead of an
    # if-else per provider.
    #
    # Two ways to fail: the model is not on our list at all, or it is on the list
    # but nobody put a key in the environment for it. Both raise the same kind of
    # error, because from the caller's side they mean the same thing — this model
    # cannot answer right now.


@traceable(
    run_type="llm",
    process_inputs=tracing.clean_inputs,
    # This one is a generator: it yields hundreds of small fragments as the model
    # types. Without `reduce_fn` the trace records a list of hundreds of scraps
    # instead of an answer. This glues them back into the string the user saw.
    reduce_fn=lambda parts: {"answer": "".join(parts)},
)
def stream_answer(model: str, messages: list[dict]):
    """Yield the answer one token at a time.

    The key is passed explicitly rather than left to LiteLLM's environment
    lookup. pydantic-settings reads `.env` into the `Settings` object but never
    exports to `os.environ`, so an implicit lookup finds the key on Railway
    (real environment variables) and misses it locally — a bug that appears on
    exactly one machine, which is the worst kind to debug.

    Raises before the first token if the model is unusable, so the router can
    still answer with an HTTP status code. Once this generator yields, the
    response status is already on the wire and an error can only be an SSE
    event.
    """
    api_key = api_key_for(model)

    response = litellm.completion(
        model=model,
        messages=messages,
        stream=True,
        api_key=api_key,
        max_tokens=MAX_ANSWER_TOKENS,
        timeout=ANSWER_TIMEOUT,
    )

    for part in response:
        # A stream carries bookkeeping chunks too: an opening chunk with a role
        # and no text, and a final one with a finish reason and no text. Both
        # arrive with `content` as None, and both are normal.
        choices = getattr(part, "choices", None)
        if not choices:
            continue
        text = choices[0].delta.content
        if text:
            yield text

    # In plain English: `stream=True` asks the provider to send the answer back
    # in pieces as it is written, rather than making us wait for the finished
    # paragraph. LiteLLM is the translator that makes Gemini and OpenAI both
    # accept the same request shape.
    #
    # `yield` instead of `return` makes this a "generator": each piece is handed
    # to whoever is looping over this function the moment it arrives, and the
    # function then pauses where it stands until asked for the next one. That is
    # what lets the browser show words appearing live.
    #
    # The two guards skip the pieces that carry no words. A stream opens with a
    # chunk that only says who is speaking and closes with one that only says why
    # it stopped; both are normal and neither should reach the screen.


@traceable(run_type="llm")
def generate_title(question: str) -> str:
    """A few words naming what a conversation is about.

    Always on `DEFAULT_MODEL`, never the model the caller chose to answer with.
    A title is a label in a sidebar; there is no reason to bill the expensive
    model for one, and no reason for the same question to produce a different
    label depending on which provider answered it.

    Not streamed — there is nobody watching six words arrive one at a time.

    Raises rather than returning a fallback. The only caller already has a
    perfectly good placeholder (the truncated first message) and is the right
    place to decide that keeping it is fine; inventing a second-best title down
    here would hide the failure from the log.
    """
    api_key = api_key_for(DEFAULT_MODEL)

    response = litellm.completion(
        model=DEFAULT_MODEL,
        messages=[
            {"role": "system", "content": TITLE_PROMPT},
            {"role": "user", "content": question},
        ],
        api_key=api_key,
        max_tokens=TITLE_TOKENS,
        timeout=HELPER_TIMEOUT,
    )

    title = (response.choices[0].message.content or "").strip().strip('"')

    if not title:
        raise ValueError("The model returned an empty title.")

    return title[:TITLE_MAX_CHARS]

    # In plain English: ask the cheap model to name the question, then take the
    # one answer it gives back. `.strip('"')` is there because models like
    # wrapping titles in quotation marks however plainly you ask them not to.
    #
    # The final slice can only fire if a model ignores `max_tokens`, but it is
    # what guarantees the string fits the same 200-character bound
    # `ConversationUpdate` enforces on a human rename — model output is untrusted
    # input to the database exactly like a request body is.


# ---------------------------------------------------------------------------
# Self-check
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Covers the branch that would otherwise fail only against a real document:
    # an image chunk has to become an image part, a text chunk must not, and the
    # numbering in the prompt has to match the numbering in the saved sources.
    # Needs no API key and no database:
    #   apps\api> .venv\Scripts\python.exe -m app.services.rag
    sample = [
        {
            "id": "c1",
            "document_id": "d1",
            "document_name": "report.pdf",
            "content": "Revenue grew 12% year over year.",
            "chunk_index": 4,
            "chunk_type": "text",
            "image_path": None,
            "page_number": 2,
            "similarity": 0.71,
        },
        {
            "id": "c2",
            "document_id": "d1",
            "document_name": "report.pdf",
            "content": "[Image from page 5]",
            "chunk_index": 5,
            "chunk_type": "image",
            "image_path": "user/doc/img-5.jpg",
            "page_number": 5,
            "similarity": 0.66,
        },
    ]
    fake_images = {"user/doc/img-5.jpg": "data:image/jpeg;base64,AAAA"}

    messages = build_messages("Did revenue grow?", sample, fake_images)

    assert messages[0]["role"] == "system", "system prompt is not first"
    assert messages[1]["role"] == "user", "sources are not on the user turn"

    parts = messages[1]["content"]
    image_parts = [p for p in parts if p["type"] == "image_url"]
    assert len(image_parts) == 1, "the image chunk did not become an image part"
    assert image_parts[0]["image_url"]["url"].startswith("data:image/jpeg;base64,"), (
        "the image part is not a data URI"
    )
    assert parts[-1]["text"].endswith("Did revenue grow?"), "the question is not last"
    assert "Revenue grew 12%" in parts[0]["text"], "the text chunk lost its content"
    assert "[1] report.pdf, page 2" in parts[0]["text"], "source 1 is mislabelled"
    assert "[2] report.pdf, page 5" in parts[1]["text"], "source 2 is mislabelled"

    # A missing image must degrade, not explode.
    degraded = build_messages("Did revenue grow?", sample, {})
    assert not [p for p in degraded[1]["content"] if p["type"] == "image_url"], (
        "an unloadable image still produced an image part"
    )

    # Day 9b: history is spliced between the rules and the sources, and adding it
    # must not disturb either end of the prompt.
    past = [
        {"role": "user", "content": "What does the report cover?"},
        {"role": "assistant", "content": "Revenue, headcount and costs [1]."},
    ]
    with_history = build_messages("Did revenue grow?", sample, fake_images, past)

    assert [m["role"] for m in with_history] == ["system", "user", "assistant", "user"], (
        "history is not sitting between the system rules and the sources turn"
    )
    assert with_history[1]["content"] == "What does the report cover?", (
        "the past user message was altered on its way into the prompt"
    )
    assert with_history[-1]["content"][-1]["text"].endswith("Did revenue grow?"), (
        "the question is no longer last once history is present"
    )
    assert build_messages("Did revenue grow?", sample, fake_images, []) == messages, (
        "an empty history changed the prompt"
    )
    assert build_messages("Did revenue grow?", sample, fake_images, None) == messages, (
        "a history-less call is no longer identical to the pre-9b prompt"
    )

    sources = to_sources(sample)
    assert [s["number"] for s in sources] == [1, 2], "source numbering drifted from the prompt"
    assert sources[1]["chunk_type"] == "image", "the image source lost its type"
    assert len(sources[0]["content_preview"]) <= PREVIEW_CHARS, "preview is not capped"

    # Without this the image endpoint has nothing to be called with, and the
    # failure would be invisible from here: the answer and its citations would
    # look exactly right, and only the picture would quietly never appear.
    assert [s["chunk_id"] for s in sources] == ["c1", "c2"], (
        "sources lost the chunk id the image endpoint is addressed by"
    )

    assert DEFAULT_MODEL in SUPPORTED_MODELS, "the default model is not in the allowlist"

    print(f"OK — {len(parts)} prompt parts, {len(image_parts)} image, {len(sources)} sources")
