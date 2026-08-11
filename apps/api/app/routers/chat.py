"""The chat endpoint: a question in, a streamed grounded answer out.

The shape of this file is decided by one fact about HTTP: the status line is the
*first* thing sent, so the moment streaming begins the response is already
committed to being a 200. An error after that point cannot be a 500 — it can
only be an SSE `error` event that the browser has to be written to understand.

So everything that can fail cleanly happens before the stream opens, in this
order, and the order is the design:

    spend cap → embed → retrieve → no chunks? 400 → load images
              → create/verify conversation → THEN StreamingResponse

The cap is first because it is free; embedding is second because it is the first
thing that costs money. Nothing below the cap runs for a caller who has had
enough for one day.

As everywhere else in this app, no query here carries a `WHERE user_id = ...`.
RLS from `001_init.sql` does that, and `match_chunks` is deliberately
`security invoker` so those same policies apply inside the vector search. See
the RLS rule in CLAUDE.md.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse

from app.config import get_settings
from app.deps import CurrentUser, SupabaseClient
from app.models.chat import ChatRequest
from app.services import rag

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])

# What a new conversation is called until Day 9 replaces it with a real title.
# The user's own first words, cut short — a placeholder that is still recognisable
# in a sidebar, at the cost of nothing.
TITLE_CHARS = 60


def _enforce_daily_limit(supabase) -> None:
    """Refuse a question once enough have been asked today.

    The twin of `_enforce_daily_limit` in `documents.py`, deliberately written
    out again rather than shared: it counts a different table, through a
    different function, against different limits, with different wording. A
    single helper taking five arguments to serve two callers would be more code
    than this, not less.

    Global limit first — if the whole service has spent enough for one day, whose
    turn it is stops mattering. It reads a count RLS would otherwise hide, via
    the `security definer` function in migration 005.

    Only `role = 'user'` rows are counted. One user message is exactly one LLM
    call; the assistant row is its result, and counting both would silently
    halve the cap.
    """
    settings = get_settings()

    try:
        today = supabase.rpc("messages_created_today").execute()
    except Exception as exc:
        logger.exception("Global chat limit check failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not check service capacity. Please try again.",
        ) from exc

    if (today.data or 0) >= settings.global_daily_message_limit:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="This demo has hit its daily limit. Please try again tomorrow.",
        )

    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

    try:
        recent = (
            supabase.table("messages")
            .select("id", count="exact")
            .eq("role", "user")
            .gte("created_at", since)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        logger.exception("Daily chat limit check failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not check your message allowance. Please try again.",
        ) from exc

    limit = settings.max_messages_per_day
    if (recent.count or 0) >= limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Daily message limit reached ({limit} questions in 24 hours). Try again tomorrow.",
        )

    # In plain English, the query above: ask Postgres to count this user's
    # questions from the last 24 hours. `count="exact"` says "tell me the total";
    # `.limit(1)` says "but don't actually send me the rows" — we want the number,
    # not the messages. RLS is what makes this the *caller's* count without us
    # naming them anywhere.


def _resolve_conversation(supabase, user_id: str, conversation_id: UUID | None, message: str) -> UUID:
    """Return the conversation this message belongs to, creating one if needed.

    Done before streaming so a bad id is a clean 404 rather than an error event
    mid-answer. Verifying an existing id is not redundant with RLS — RLS would
    reject the message insert anyway, but that happens *after* the answer has
    been generated and paid for.
    """
    if conversation_id is None:
        new_id = uuid4()
        row = {
            "id": str(new_id),
            # From the verified token, never from the request body. RLS re-checks
            # it, so this cannot mint a conversation belonging to someone else.
            "user_id": user_id,
            "title": message[:TITLE_CHARS],
        }
        try:
            created = supabase.table("conversations").insert(row).execute()
        except Exception as exc:
            logger.exception("Could not create a conversation")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Could not start the conversation. Please try again.",
            ) from exc

        if not created.data:
            # PostgREST returns an empty set, not an error, when an RLS
            # WITH CHECK rejects the row.
            logger.error("Conversation insert returned no row")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Could not start the conversation. Please try again.",
            )

        return new_id

    try:
        found = (
            supabase.table("conversations")
            .select("id")
            .eq("id", str(conversation_id))
            .execute()
        )
    except Exception as exc:
        logger.exception("Conversation lookup failed for %s", conversation_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not open the conversation. Please try again.",
        ) from exc

    # RLS scopes the select to the caller, so somebody else's conversation is
    # indistinguishable from one that does not exist — the same 404 both ways,
    # which reveals less than a 403 would.
    if not found.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found.",
        )

    return conversation_id


def _save_exchange(
    supabase,
    user_id: str,
    conversation_id: UUID,
    question: str,
    answer: str,
    model: str,
    sources: list[dict],
) -> None:
    """Write the question and the answer, then touch the conversation.

    Both rows in one insert: they are one exchange, and a half-saved exchange is
    a conversation that reads as though the assistant spoke first. `sources`
    rides on the assistant row because it describes that answer — Day 8 renders
    it directly from here rather than searching again.

    Deliberately swallows its own failure. The user has already read the answer;
    raising now would replace a saved-or-not question with an error banner
    underneath text they can plainly see. It is logged loudly instead.
    """
    rows = [
        {
            "user_id": user_id,
            "conversation_id": str(conversation_id),
            "role": "user",
            "content": question,
        },
        {
            "user_id": user_id,
            "conversation_id": str(conversation_id),
            "role": "assistant",
            "content": answer,
            "model": model,
            "sources": sources,
        },
    ]

    try:
        supabase.table("messages").insert(rows).execute()
        # Without this, `updated_at` records when the conversation was created
        # and never moves again — so a sidebar sorted by recent activity would
        # be sorted by nothing.
        supabase.table("conversations").update(
            {"updated_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", str(conversation_id)).execute()
    except Exception:
        logger.exception("Could not save the exchange in conversation %s", conversation_id)


def _event(name: str, payload: dict) -> str:
    """One server-sent event, as the wire format wants it.

    The blank line at the end is not cosmetic — it is what tells the browser the
    event is complete. The payload is JSON-encoded for a harder reason: SSE is
    line-based, so a raw newline inside `data:` would be read as the end of the
    message. Answers contain newlines constantly. JSON turns them into `\\n`, two
    ordinary characters, which is what makes streaming prose safe at all.
    """
    return f"event: {name}\ndata: {json.dumps(payload)}\n\n"


# Deliberately `def`, not `async def`, exactly like `ingest_step`. Everything in
# here is a blocking network call — Cohere, Supabase, then the model provider
# token by token. Inside an `async def` those would freeze the event loop and
# every other user's request along with it. FastAPI runs a sync handler in a
# worker thread, so the loop stays free.
@router.post("")
def chat(
    user_id: CurrentUser,
    supabase: SupabaseClient,
    request: ChatRequest,
) -> StreamingResponse:
    """Answer a question from the caller's own documents, streaming the reply."""
    _enforce_daily_limit(supabase)

    # Before anything is spent: refuse a model this server has no key for. Inside
    # the generator this same failure could only be an SSE error event, after the
    # question had already been embedded and searched.
    try:
        rag.api_key_for(request.model)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    try:
        query_vector = rag.embed_query(request.message)
    except Exception as exc:
        logger.exception("Embedding the question failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not understand the question right now. Please try again.",
        ) from exc

    try:
        chunks = rag.retrieve(supabase, query_vector)
    except Exception as exc:
        logger.exception("Vector search failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not search your documents. Please try again.",
        ) from exc

    if not chunks:
        # Nothing to ground an answer in. Asking the model anyway would produce a
        # confident answer from its own training data, which is the exact failure
        # this whole app exists to avoid.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No documents to search yet. Upload one and wait for it to finish processing.",
        )

    images = rag.load_images(supabase, chunks)
    messages = rag.build_messages(request.message, chunks, images)
    sources = rag.to_sources(chunks)

    conversation_id = _resolve_conversation(
        supabase, user_id, request.conversation_id, request.message
    )

    def generate():
        """Emit the answer as SSE, then save it.

        `conversation` goes first so a browser that started without an id learns
        it immediately, and `sources` before any token so citations can be
        rendered as `[1]` appears rather than after the answer settles.
        """
        answer: list[str] = []

        try:
            yield _event("conversation", {"id": str(conversation_id)})
            yield _event("sources", {"sources": sources})

            for text in rag.stream_answer(request.model, messages):
                answer.append(text)
                yield _event("token", {"text": text})
        except Exception:
            logger.exception("Generation failed in conversation %s", conversation_id)
            # No exception text: a provider error can echo request details, and
            # this string is going to a browser.
            yield _event("error", {"detail": "The answer stopped early. Please try again."})
            return

        full = "".join(answer)
        if not full.strip():
            # A model that returned nothing must not leave a blank bubble in the
            # history forever. Report it, save nothing.
            logger.error("Model %s returned an empty answer", request.model)
            yield _event("error", {"detail": "The model returned an empty answer. Please try again."})
            return

        _save_exchange(
            supabase, user_id, conversation_id, request.message, full, request.model, sources
        )
        yield _event("done", {})

        # In plain English, the block above: collect every piece of text as it
        # streams past, sending each one to the browser the moment it arrives.
        # `yield` hands a piece over and pauses here until the browser is ready
        # for the next, which is what makes words appear live instead of all at
        # once.
        #
        # If the provider fails halfway, we cannot send an error *status* — the
        # response started with "200 OK" seconds ago — so we send an error
        # *event* and stop. Only once the answer is complete do we glue the
        # pieces back into one string and store it.

    # ponytail: a browser that disconnects mid-answer loses the exchange — the
    # generator is closed and `_save_exchange` never runs. Accepted: the user saw
    # a partial answer they did not keep. Wrap the loop in try/finally if it ever
    # matters.
    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # The one that actually matters in production. Nginx-family proxies
            # buffer a response by default, which would hold every token until
            # the answer was finished and turn streaming into a long pause
            # followed by a wall of text — indistinguishable from no streaming.
            "X-Accel-Buffering": "no",
        },
    )
