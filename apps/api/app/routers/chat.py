"""The chat endpoint: a question in, a streamed grounded answer out.

The shape of this file is decided by one fact about HTTP: the status line is the
*first* thing sent, so the moment streaming begins the response is already
committed to being a 200. An error after that point cannot be a 500 — it can
only be an SSE `error` event that the browser has to be written to understand.

So everything that can fail cleanly happens before the stream opens, in this
order, and the order is the design:

    spend cap → load history → rewrite the question → embed → retrieve
              → no chunks? 400 → load images → create/verify conversation
              → THEN StreamingResponse

The cap is first because it is free; everything below it costs money, so nothing
below it runs for a caller who has had enough for one day.

History and the rewrite come *before* the embed and that ordering is the whole of
Day 9b: a follow-up like "give count" carries no subject, and once it has been
turned into a vector it is too late for any prompt downstream to recover what it
meant.

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
from app.services import rag, tracing

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])

# What a new conversation is called until `_retitle` names it properly at the end
# of the first answer. The user's own first words, cut short. Still worth writing
# even though it usually lives for seconds: it costs nothing, it is what the row
# is called while the answer streams, and it is what the conversation keeps
# forever if the titling call fails.
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


def _resolve_conversation(
    supabase, user_id: str, conversation_id: UUID | None, message: str
) -> tuple[UUID, bool]:
    """Return the conversation this message belongs to, and whether it is new.

    Done before streaming so a bad id is a clean 404 rather than an error event
    mid-answer. Verifying an existing id is not redundant with RLS — RLS would
    reject the message insert anyway, but that happens *after* the answer has
    been generated and paid for.

    The second half of the return value exists for auto-titling. This function is
    the only place that knows whether a row was inserted or looked up, and
    without it the caller would have to re-derive the answer or pay to re-title
    a conversation on every message in it.
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

        return new_id, True

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

    return conversation_id, False


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


def _retitle(supabase, conversation_id: UUID, question: str) -> str | None:
    """Replace a new conversation's placeholder title with a written one.

    Returns the new title, or `None` if anything at all went wrong — and the
    caller is expected to shrug at `None`. By the time this runs the answer is
    already on the user's screen and already saved; failing to name it well is
    not a reason to show an error underneath text that plainly worked. The
    placeholder from `_resolve_conversation` stays, which is why there is
    something to fall back to.

    Same swallow-and-log bargain `_save_exchange` makes, for the same reason.
    """
    try:
        title = rag.generate_title(question)
    except Exception:
        logger.exception("Could not generate a title for conversation %s", conversation_id)
        return None

    try:
        supabase.table("conversations").update({"title": title}).eq(
            "id", str(conversation_id)
        ).execute()
    except Exception:
        logger.exception("Could not save the title for conversation %s", conversation_id)
        return None

    return title

    # `updated_at` is left alone deliberately, the same as a manual rename in
    # `conversations.py`. It means "when was this last talked in" and the sidebar
    # sorts by it; `_save_exchange` has already moved it to now, and touching it
    # again here would be a second write saying nothing new.


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

    # The trace opens here rather than at the top of the function on purpose. The
    # two guards above refuse before a cent is spent, and a trace should mean "a
    # real attempt happened", not "somebody knocked". `conversation_id` is "new"
    # when the request carries none; the resolved UUID is attached when the root
    # is closed, by which point `_resolve_conversation` has produced it.
    root = tracing.start_root(
        name="chat_query",
        inputs={"question": request.message},
        metadata={
            "user_id": user_id,
            "model": request.model,
            "conversation_id": str(request.conversation_id or "new"),
        },
        tags=[request.model],
    )

    # `tracing.parent(root)` is not decoration, and leaving it out is the quiet
    # failure mode: `start_root` only builds the run object, it sets nothing that
    # the `@traceable` decorators in rag.py can see. Without this block each of
    # them would open its own top-level trace and one question would arrive in
    # the dashboard as six unrelated fragments — no error, just a useless
    # dashboard.
    #
    # The try/except exists because a hand-made root is closed by nobody. The
    # three `raise HTTPException` paths below would otherwise leave a run marked
    # "running" in the UI forever, which reads as a hung request — worse than no
    # trace at all. One handler, not three: `repr()` on an HTTPException already
    # prints its status and detail, and this text goes to our own dashboard, not
    # to a browser.
    try:
        with tracing.parent(root):
            # Day 9b. Deliberately a plain select rather than
            # `_resolve_conversation`, which stays where it is further down: that
            # function *creates* the row, and moving it above the embed call
            # would leave an empty conversation in the sidebar every time
            # embedding failed. A bogus or foreign id costs one indexed lookup
            # here, returns nothing (RLS), and still gets its clean 404 from
            # `_resolve_conversation` later.
            history: list[dict] = []
            if request.conversation_id is not None:
                try:
                    history = rag.load_history(supabase, request.conversation_id)
                except Exception:
                    # Degrade to Day 9a: an answer with no memory beats an error page.
                    logger.exception("Could not load history for %s", request.conversation_id)

            search_query = request.message
            if history:
                try:
                    search_query = rag.rewrite_query(request.message, history)
                    # Kept alongside the span: Railway logs are what you reach
                    # for when LangSmith is switched off or unreachable, and this
                    # rewrite is otherwise invisible — never sent to the
                    # answering model, never saved, never shown to the user.
                    logger.info("Rewrote %r as %r", request.message, search_query)
                except Exception:
                    logger.exception("Query rewrite failed; searching the original question")

            # In plain English: if this message belongs to an existing
            # conversation, read the last few messages of it. If there are any,
            # ask the cheap model to turn the question into one that makes sense
            # on its own — "give count" becomes "how many factors affect
            # positioning accuracy?" — and search with that instead. A first
            # message has no history, so both steps are skipped and cost nothing.
            # If either step breaks we keep the original question and carry on,
            # which is exactly how this endpoint behaved yesterday.

            try:
                query_vector = rag.embed_query(search_query)
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
                # Nothing to ground an answer in. Asking the model anyway would
                # produce a confident answer from its own training data, which is
                # the exact failure this whole app exists to avoid.
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="No documents to search yet. Upload one and wait for it to finish processing.",
                )

            images = rag.load_images(supabase, chunks)
            # `request.message`, never `search_query`. The user asked "give
            # count" and that is the question the model answers; the rewrite
            # existed only to produce a better vector, and it has already done
            # that. History is in the prompt to make the original question
            # legible.
            messages = rag.build_messages(request.message, chunks, images, history)
            sources = rag.to_sources(chunks)

            conversation_id, is_new = _resolve_conversation(
                supabase, user_id, request.conversation_id, request.message
            )
    except Exception as exc:
        tracing.finish_root(root, error=repr(exc))
        raise

    # In plain English, the two lines around all of that: open one record for
    # this whole question and keep hold of it. Everything indented underneath
    # gets filed inside that record rather than lying around loose. If any of it
    # fails, we close the record with the reason before letting the failure carry
    # on to the user — a record nobody closed sits in the dashboard claiming the
    # request is still running, which is more misleading than having no record.

    def generate():
        """Emit the answer as SSE, then save it.

        `conversation` goes first so a browser that started without an id learns
        it immediately, and `sources` before any token so citations can be
        rendered as `[1]` appears rather than after the answer settles.
        """
        answer: list[str] = []
        # What the trace says happened. `failure` stays None on a clean answer;
        # `finished` distinguishes "we sent everything" from "the browser went
        # away", which have no other way of telling themselves apart down here.
        failure: str | None = None
        finished = False

        try:
            yield _event("conversation", {"id": str(conversation_id)})
            yield _event("sources", {"sources": sources})

            # This `with` goes around the loop and no higher, and the placement
            # is the whole trick. Starlette pulls this generator one chunk at a
            # time, and each pull is a separate hop into the thread pool carrying
            # a *fresh copy* of the invisible context — so a block opened above
            # the first `yield` has already been forgotten by the time the loop
            # starts. Here, entering the block and pulling `stream_answer`'s
            # first token happen in the same hop, and that first pull is the
            # moment `@traceable` decides who its parent is. From then on the
            # span carries its own parent and stops caring about the context.
            with tracing.parent(root):
                for text in rag.stream_answer(request.model, messages):
                    answer.append(text)
                    yield _event("token", {"text": text})

            full = "".join(answer)
            if not full.strip():
                # A model that returned nothing must not leave a blank bubble in
                # the history forever. Report it, save nothing.
                logger.error("Model %s returned an empty answer", request.model)
                failure = "The model returned an empty answer"
                yield _event("error", {"detail": "The model returned an empty answer. Please try again."})
                return

            _save_exchange(
                supabase, user_id, conversation_id, request.message, full, request.model, sources
            )

            if is_new:
                # Its own block: `generate_title` runs in a later hop than the
                # loop above, so the parent has to be re-established or its span
                # floats off on its own. No `yield` inside, so nothing can
                # suspend it half-way.
                with tracing.parent(root):
                    title = _retitle(supabase, conversation_id, request.message)
                if title:
                    yield _event("title", {"title": title})

            finished = True
            yield _event("done", {})
        except Exception:
            logger.exception("Generation failed in conversation %s", conversation_id)
            failure = "Generation failed"
            # No exception text: a provider error can echo request details, and
            # this string is going to a browser.
            yield _event("error", {"detail": "The answer stopped early. Please try again."})
        finally:
            # Runs on every path that reaches the end of this generator: a
            # finished answer, an empty one, or a provider failure.
            #
            # It does NOT run on client disconnect, which was the original reason
            # for writing it. Measured 2026-08-14, not assumed: Starlette's
            # `iterate_in_threadpool` (concurrency.py:51-59) pulls this generator
            # but has no `finally` closing it, and on disconnect `stream_response`
            # raises `ClientDisconnect` and abandons it mid-`yield`. Nothing
            # throws GeneratorExit in, so this block is never entered and the
            # trace stays "running" in the dashboard. `finished` is still set and
            # read here so that whatever eventually collects the generator records
            # the truth. Fixing the abandonment has to happen outside this
            # function — see the note below the return.
            if failure is None and not finished:
                failure = "Client disconnected before the answer finished"
            tracing.finish_root(
                root,
                outputs={"answer": "".join(answer)},
                error=failure,
                metadata={"conversation_id": str(conversation_id)},
            )

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
        #
        # The `finally` at the end runs no matter how we leave — finished,
        # failed, or you closed the tab — and its one job is to write down how
        # this question ended before the record is filed away.

    # ponytail: a browser that disconnects mid-answer is abandoned, not closed.
    # Measured 2026-08-14: Starlette never closes this generator, so `generate()`
    # is left suspended at a `yield` and THREE things follow from that one cause —
    # `_save_exchange` never runs, the `finally` above never runs so the trace
    # stays "running", and the model stream stays open and billable until the
    # garbage collector happens to get to it. Left alone deliberately in 10a: the
    # fix belongs outside this function and lands with the `_save_exchange`
    # decision in 10b. Upgrade path: close the root from an ASGI middleware, or
    # hand Starlette a wrapper object that owns closing the generator.
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
