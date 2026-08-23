"""The chat endpoint: a question in, a streamed grounded answer out.

The shape of this file is decided by one fact about HTTP: the status line is the
*first* thing sent, so the moment streaming begins the response is already
committed to being a 200. An error after that point cannot be a 500 — it can
only be an SSE `error` event that the browser has to be written to understand.

So everything that can fail cleanly happens before the stream opens, in this
order, and the order is the design:

    spend cap → load history → rewrite the question → embed → retrieve
              → no chunks? 400 → load images → create/verify conversation
              → record the question → spend cap again
              → THEN StreamingResponse

The cap is first because it is free; everything below it costs money, so nothing
below it runs for a caller who has had enough for one day.

The cap appears *twice*, and the second one is the one that binds. The count it
reads is of `messages` rows, and this request only adds its own row partway
down — so the first check reads a number this request has not yet moved, and a
burst of simultaneous tabs would all read the same stale value and all pass.
Recording the question before the model is called is what lets concurrent
requests see one another, and it is also what stops a paid call that later
fails to save from costing nothing against the allowance.

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
import time
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import litellm
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from app.config import get_settings
from app.deps import CurrentUser, SupabaseClient, TokenExpiry
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

# The answer has to finish while the caller's token is still alive, because
# `_save_answer` and `_retitle` ride on that same token — the Supabase client
# carries it verbatim, which is what makes RLS work. Clerk hands the browser a
# *cached* token and refreshes it only near expiry, so a request routinely
# arrives with twenty seconds of life rather than a full minute. Without a
# deadline the answer reaches the screen complete, the insert then 401s,
# `_save_answer` swallows it, and the answer is gone on reload.
#
# The margin is the gap left between the last token we emit and the moment the
# credential dies, so the two writes underneath have room to land.
TOKEN_SAFETY_MARGIN_SECONDS = 5

# Below this much usable life, refuse before spending anything. A 401 is what
# makes the browser fetch a fresh token and replay the request — `fetchWithToken`
# in apps/web/src/lib/api.ts already does exactly that — which costs one round
# trip and shows nothing on screen.
#
# Deliberately *not* `ANSWER_TIMEOUT`, which is the obvious reading and is
# unsatisfiable: Clerk's session tokens live 60 seconds and `ANSWER_TIMEOUT` is
# 60, so a brand new token would fail the test too and every request would 401
# forever. This floor asks only "is there time to say anything useful".
MIN_ANSWER_BUDGET_SECONDS = 15


def _enforce_daily_limit(supabase, *, already_counted: bool = False) -> None:
    """Refuse a question once enough have been asked today.

    The twin of `_enforce_daily_limit` in `documents.py`, deliberately written
    out again rather than shared: it counts a different table, through a
    different function, against different limits, with different wording. A
    single helper taking five arguments to serve two callers would be more code
    than this, not less.

    Global limit first — if the whole service has spent enough for one day, whose
    turn it is stops mattering. It reads a count RLS would otherwise hide, via
    the `security definer` function in migration 005.

    **Called twice per request, and that is what makes the cap real.** The first
    call is free and refuses before a cent is spent. The second runs *after*
    this question's own row has been written, which is the half that holds
    against a burst: the count used to move only at the very *end* of a
    request, so twenty tabs submitted together all read the same stale number
    and all passed. Now each request writes its row first and then re-reads, so
    they can see one another.

    `already_counted` says whether our own row is in the number yet. When it is,
    the caps become "more than" rather than "at least" — otherwise the 30th
    question of a 30-a-day allowance would refuse itself.

    Only `role = 'user'` rows are counted, which makes this a cap on *questions
    asked*, not on model calls billed. Since Day 9b those are no longer the same
    thing: one question can bill up to three completions — `rewrite_query`, the
    answer itself, and `generate_title`. The note that used to sit here claimed
    the opposite ("one user message is exactly one LLM call"), which is exactly
    the sort of confident comment that stops anyone checking. Whether
    `max_messages_per_day` should come down to match is a spending decision, not
    a code one, so it is left alone here.
    """
    settings = get_settings()

    # Room for this request's own row when it has already been written.
    allowance = 1 if already_counted else 0

    try:
        today = supabase.rpc("messages_created_today").execute()
    except Exception as exc:
        logger.exception("Global chat limit check failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not check service capacity. Please try again.",
        ) from exc

    if (today.data or 0) >= settings.global_daily_message_limit + allowance:
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
    if (recent.count or 0) >= limit + allowance:
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


def _record_question(
    supabase, user_id: str, conversation_id: UUID, question: str
) -> None:
    """Write the user's message *before* the answer is paid for.

    This used to go in one insert together with the answer, at the end. Moving
    it up here is the load-bearing half of the daily cap: the row is the thing
    the cap counts, so writing it last meant a paid call that then failed to
    save left the counter untouched — free retries, indefinitely — and a burst
    of simultaneous requests each counted a number that none of them had moved
    yet.

    Unlike `_save_answer` below, this one raises rather than swallowing.
    Nothing has been spent at this point, so a failure costs the caller one
    retry, whereas carrying on would buy an answer the cap could never see.

    The trade-off accepted: a question whose answer then fails stays in the
    history with no reply under it. That is the right way round — it is what
    actually happened, it is honest about what was spent, and asking again
    simply adds the next exchange.
    """
    try:
        supabase.table("messages").insert(
            {
                # From the verified token, never the request body. RLS re-checks
                # it, so this cannot write into somebody else's conversation.
                "user_id": user_id,
                "conversation_id": str(conversation_id),
                "role": "user",
                "content": question,
            }
        ).execute()
    except Exception as exc:
        logger.exception("Could not record the question in conversation %s", conversation_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not send your question. Please try again.",
        ) from exc


def _discard_conversation(supabase, conversation_id: UUID) -> None:
    """Delete a conversation that was created moments ago and will hold nothing.

    The `conversations` row is inserted before the stream opens so the browser
    can be handed its id immediately. Anything failing between there and the
    first saved message used to leave a titled conversation with zero messages
    in it, sitting in the sidebar forever, opening onto nothing, and never
    retitled.

    Only called where nothing was answered *and* nothing was recorded, so there
    is genuinely nothing to lose. `messages` is `on delete cascade`
    (001_init.sql), so a question written a moment earlier goes with it.

    Swallows its own failure: the request is already failing for a reason the
    caller cares about far more than sidebar tidiness, and that reason must not
    be replaced by this one.
    """
    try:
        supabase.table("conversations").delete().eq(
            "id", str(conversation_id)
        ).execute()
    except Exception:
        logger.exception("Could not discard the empty conversation %s", conversation_id)


def _save_answer(
    supabase,
    user_id: str,
    conversation_id: UUID,
    answer: str,
    model: str,
    sources: list[dict],
    run_id: str | None,
) -> None:
    """Write the answer, then touch the conversation.

    The question is already in the table — `_record_question` put it there
    before the model was called — so this writes the other half. That ordering
    also means a half-saved exchange can now only ever be a question without an
    answer, never an answer with no question above it.

    `sources` rides on the assistant row because it describes that answer —
    Day 8 renders it directly from here rather than searching again.

    Deliberately swallows its own failure. The user has already read the answer;
    raising now would replace a saved-or-not question with an error banner
    underneath text they can plainly see. It is logged loudly instead.
    """
    row = {
        "user_id": user_id,
        "conversation_id": str(conversation_id),
        "role": "assistant",
        "content": answer,
        "model": model,
        "sources": sources,
        # Only on the assistant row: it names the pipeline run that produced
        # this text, and a question was not produced by one. Null whenever
        # tracing is off, which is what keeps saving an answer independent of
        # whether the observability vendor is switched on.
        "run_id": run_id,
    }

    try:
        supabase.table("messages").insert(row).execute()
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

    Same swallow-and-log bargain `_save_answer` makes, for the same reason.
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
    # sorts by it; `_save_answer` has already moved it to now, and touching it
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


def _safe_trace_error(exc: BaseException) -> str:
    """What may be written onto a trace that could later be shared publicly.

    A trace is shareable — the README is meant to link one — which is why the
    Clerk id is hashed with `tracing.anon` further up rather than sent raw. This
    field was the hole in that reasoning. It used to be `repr(exc)`, defended on
    the grounds that an `HTTPException`'s repr is only a status and a message we
    wrote ourselves. True for the three `raise HTTPException` paths, but not for
    everything reaching that handler: `rag.embed`, `load_images`,
    `build_messages`, `to_sources` and the conversation writes are all unwrapped,
    so a supabase or httpx repr can land here carrying the request it just tried
    to make.

    So: our own text passes through, since it was written to be read by users.
    Anything else is reduced to its class name, which still says what broke
    without quoting the failing call back at a public page. The detail is not
    lost — it goes to the server log, which nobody can share by accident.

    No key has been found that could leak here (supabase-py sends credentials as
    headers, not in messages), so this is about request detail rather than
    secrets. That is worth fixing on a page meant to be public, and not worth
    pretending is worse than it is.
    """
    if isinstance(exc, HTTPException):
        return f"HTTPException {exc.status_code}: {exc.detail}"
    return type(exc).__name__


# What the reader is told when an answer dies half-written, by cause.
#
# Every one of these used to be "The answer stopped early. Please try again." —
# which is advice, and for three of the five it is *wrong* advice. Trying again
# immediately makes a rate limit worse; it cannot help a revoked key; and it
# will fail identically on a conversation that has outgrown the model's context.
# BUILD.md's step 7 names exactly these four, and this is where they land.
#
# Ordered, and the order is load-bearing: `ContextWindowExceededError` and
# `ContentPolicyViolationError` are both subclasses of `BadRequestError`
# (checked against the installed litellm, not assumed), so a `BadRequestError`
# entry placed above them would swallow both and report the generic message.
#
# The strings are ours. None of them interpolates the provider's text, for the
# same reason `_safe_trace_error` exists: a provider message can quote the
# request it just made, and this one is going to a browser.
_STREAM_FAILURES: tuple[tuple[type[Exception], str, str], ...] = (
    (
        litellm.ContextWindowExceededError,
        "context window exceeded",
        "This conversation has grown too long for the model. "
        "Start a new chat to carry on.",
    ),
    (
        litellm.ContentPolicyViolationError,
        "content policy",
        "The model refused to answer this one. Try rewording the question.",
    ),
    (
        litellm.AuthenticationError,
        "provider rejected our credentials",
        "The answer service is misconfigured. This is our problem, not yours — "
        "please try again later.",
    ),
    (
        litellm.PermissionDeniedError,
        "provider denied permission",
        "The answer service is misconfigured. This is our problem, not yours — "
        "please try again later.",
    ),
    (
        litellm.RateLimitError,
        "provider rate limit",
        "The answer service is busy right now. Wait a moment and ask again.",
    ),
    (
        litellm.Timeout,
        "provider timed out",
        "The model took too long to answer. Please try again.",
    ),
    (
        litellm.ServiceUnavailableError,
        "provider unavailable",
        "The answer service is temporarily unavailable. Please try again shortly.",
    ),
    (
        litellm.APIConnectionError,
        "could not reach provider",
        "Could not reach the answer service. Please try again shortly.",
    ),
)


def _stream_failure(exc: BaseException) -> tuple[str, str]:
    """(what the trace records, what the reader is told) for a dead stream.

    Two strings rather than one because they have different audiences and
    different rules. The trace label is for the dashboard and may name the
    cause plainly; the reader's line has to be actionable, and must never carry
    provider text.
    """
    for kind, label, detail in _STREAM_FAILURES:
        if isinstance(exc, kind):
            return label, detail

    return "Generation failed", "The answer stopped early. Please try again."

    # In plain English: work down the list looking for the first entry whose
    # error type matches what actually went wrong, and use its pair of
    # sentences. If nothing matches, fall back to the old generic pair — an
    # unknown failure is still a failure, and saying something vague is better
    # than saying something confidently wrong.


# In plain English: when something goes wrong we write the reason onto the
# record of this question. That record can be shared with anyone. So we write
# our own error messages out in full — we wrote them for people to read — but
# for anything thrown by a library we write down only what *kind* of error it
# was, not its description, because those descriptions tend to quote the
# database query or web request that failed. The full text still goes to the
# private server log.


# Deliberately `def`, not `async def`, exactly like `ingest_step`. Everything in
# here is a blocking network call — Cohere, Supabase, then the model provider
# token by token. Inside an `async def` those would freeze the event loop and
# every other user's request along with it. FastAPI runs a sync handler in a
# worker thread, so the loop stays free.
@router.post("")
def chat(
    user_id: CurrentUser,
    supabase: SupabaseClient,
    expires_at: TokenExpiry,
    request: ChatRequest,
    # The ASGI request, not the JSON body — `request` above is already taken by
    # the body. Needed only to hand this response's generator to the
    # `close_on_disconnect` middleware; see the bottom of this function.
    http_request: Request,
) -> StreamingResponse:
    """Answer a question from the caller's own documents, streaming the reply."""
    # First, before the cap check and before a cent is spent: is this token
    # going to live long enough to save what it pays for? `expires_at` is
    # wall-clock, so it is compared against `time.time()`.
    answer_budget = expires_at - time.time() - TOKEN_SAFETY_MARGIN_SECONDS
    if answer_budget < MIN_ANSWER_BUDGET_SECONDS:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session token is about to expire. Please try again.",
            headers={"WWW-Authenticate": "Bearer"},
        )

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
            # Hashed, not raw. A trace can be made public for the README, and a
            # raw Clerk `sub` is a stable identifier. The hash still separates one
            # user's traces from another's, which is all this tag is for.
            "user_id": tracing.anon(user_id),
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
    # trace at all. One handler, not three.
    #
    # It used to write `repr(exc)`, reasoning that an HTTPException's repr is
    # just a status and a message of ours, bound for our own dashboard. Both
    # halves were too generous: most of the block is unwrapped library calls, and
    # a trace is shareable by design. See `_safe_trace_error`.
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
                chunks = rag.retrieve(supabase, query_vector, search_query, k=rag.RERANK_CANDIDATES)
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

            try:
                chunks = rag.rerank(search_query, chunks)
            except Exception as exc:
                logger.exception("Reranking failed")
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Could not rank your documents. Please try again.",
                ) from exc

            # Below this, even the best reranked chunk isn't about the
            # question — skip images, the prompt, and the paid LLM call
            # entirely. `generate()` below checks this same flag to emit
            # `rag.ABSTAIN_MESSAGE` instead of calling `stream_answer`.
            abstain = chunks[0]["similarity"] < rag.ABSTAIN_THRESHOLD

            if abstain:
                images, messages, sources = {}, [], []
            else:
                images = rag.load_images(supabase, chunks)
                # `request.message`, never `search_query`. The user asked "give
                # count" and that is the question the model answers; the rewrite
                # existed only to produce a better vector, and it has already
                # done that. History is in the prompt to make the original
                # question legible.
                messages = rag.build_messages(request.message, chunks, images, history)
                sources = rag.to_sources(chunks)

            conversation_id, is_new = _resolve_conversation(
                supabase, user_id, request.conversation_id, request.message
            )

            try:
                _record_question(supabase, user_id, conversation_id, request.message)
                # The cap check that actually binds. The one at the top of this
                # handler read a count that this request had not yet moved, so
                # simultaneous requests could not see each other; this one runs
                # with our own row already written.
                _enforce_daily_limit(supabase, already_counted=True)
            except Exception:
                if is_new:
                    # Nothing asked, nothing answered, and a conversation we
                    # minted seconds ago. Leaving it would put an empty titled
                    # row in the sidebar that opens onto nothing.
                    _discard_conversation(supabase, conversation_id)
                raise
    except Exception as exc:
        # Logged before it is trimmed, and only when it is not one of ours: an
        # `HTTPException` raised above is a decision, not a defect, and its text
        # survives into the trace anyway. Everything else loses its message on
        # the way to a shareable page, so this is the one place that detail is
        # kept — in the server log, which is private.
        if not isinstance(exc, HTTPException):
            logger.exception("Chat setup failed before the response started")
        tracing.finish_root(root, error=_safe_trace_error(exc))
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
        # The wall-clock moment after which we stop pulling tokens, so the two
        # writes below still run on a valid credential. Computed once, out here,
        # rather than per token.
        deadline = expires_at - TOKEN_SAFETY_MARGIN_SECONDS
        cut_short = False
        # Whether the answer already has a row. Read by the `finally`, which now
        # runs on disconnect too and must not file a second copy of an answer
        # that was saved normally.
        saved = False

        try:
            # `run_id` rides along here rather than in an event of its own: this
            # is already the "here is what identifies this exchange" event, and it
            # arrives before the first token, which is when the browser needs it
            # to attach a rating to the right answer. `None` when tracing is off —
            # there is no trace to rate, and the UI hides the buttons.
            yield _event(
                "conversation",
                {"id": str(conversation_id), "run_id": str(root.id) if root else None},
            )
            yield _event("sources", {"sources": sources})

            if abstain:
                # No provider call to make: the outcome is already decided,
                # and paying for one anyway is exactly what the threshold
                # exists to avoid. One event stands in for the whole loop
                # below — the browser can't tell the difference either way,
                # since both paths just emit `token` events.
                answer.append(rag.ABSTAIN_MESSAGE)
                yield _event("token", {"text": rag.ABSTAIN_MESSAGE})
            else:
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
                        if time.time() >= deadline:
                            # Stop mid-answer rather than let the save below fail.
                            # A cut-off answer that is on screen *and* in the
                            # history beats a complete one that vanishes on reload.
                            logger.warning(
                                "Answer cut short in conversation %s: token expiring",
                                conversation_id,
                            )
                            cut_short = True
                            break

            full = "".join(answer)
            if not full.strip():
                # A model that returned nothing must not leave a blank bubble in
                # the history forever. Report it, save nothing.
                logger.error("Model %s returned an empty answer", request.model)
                failure = "The model returned an empty answer"
                yield _event("error", {"detail": "The model returned an empty answer. Please try again."})
                return

            _save_answer(
                supabase,
                user_id,
                conversation_id,
                full,
                request.model,
                sources,
                str(root.id) if root else None,
            )
            saved = True

            if cut_short:
                # Saved first, then reported — that order is the point of the
                # whole change. The placeholder title stays: `_retitle` is
                # another billed completion on a token with seconds left, and a
                # well-named conversation is worth much less than the answer
                # actually landing in it.
                failure = "Cut short: caller's token was about to expire"
                yield _event(
                    "error",
                    {
                        "detail": "The answer was cut short so it could be saved. "
                        "Ask again to carry on."
                    },
                )
                return

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
        except Exception as exc:
            logger.exception("Generation failed in conversation %s", conversation_id)
            # Still no exception text on the wire — a provider error can echo
            # request details and this string is going to a browser. What
            # changed is that the *cause* now picks which of our own sentences
            # is sent, so "wait a moment" and "start a new chat" reach the
            # people they actually help instead of one line of advice that was
            # wrong for most of them.
            failure, detail = _stream_failure(exc)
            yield _event("error", {"detail": detail})
        finally:
            # Runs on every path out of this generator: a finished answer, an
            # empty one, a provider failure — and, since finding 30 was fixed,
            # a browser that went away.
            #
            # That last one used not to reach here at all. Measured 2026-08-14,
            # not assumed: Starlette's `iterate_in_threadpool`
            # (concurrency.py:51-59) pulls this generator but has no `finally`
            # closing it, so on disconnect it was abandoned mid-`yield`, nothing
            # threw `GeneratorExit` in, and this block never ran. The fix is the
            # `close_on_disconnect` middleware in main.py, which calls `.close()`
            # on this generator when the request ends however it ends. `close()`
            # raises `GeneratorExit` at the suspended `yield`, which lands here.
            #
            # `GeneratorExit` inherits from `BaseException`, not `Exception`, so
            # it goes straight past the handler above without being mistaken for
            # a provider failure — and nothing in this block may `yield`, or
            # closing would raise `RuntimeError`.
            if failure is None and not finished:
                failure = "Client disconnected before the answer finished"

            # The answer the reader never got to see the end of. Saving it is
            # the point rather than a nicety: the same reasoning the cut-short
            # path already follows, that a partial answer you can find again
            # beats a whole one that vanished. `saved` stops a second copy on
            # the normal path, and `.strip()` keeps the deliberate decision
            # above — an empty answer is reported and not stored — intact.
            full_answer = "".join(answer)
            if not saved and full_answer.strip():
                _save_answer(
                    supabase,
                    user_id,
                    conversation_id,
                    full_answer,
                    request.model,
                    sources,
                    str(root.id) if root else None,
                )
                saved = True

            tracing.finish_root(
                root,
                outputs={"answer": full_answer},
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
        # The deadline check inside the loop: the pass we were given at the door
        # expires, and we need it to still work at the end in order to file the
        # answer away. So we watch the clock while the words arrive, and if we
        # get within five seconds of the pass expiring we stop the answer where
        # it is, file what we have, and say plainly that it was cut short. Half
        # an answer you can still find tomorrow is worth more than a whole one
        # that disappears the moment you reload the page — which is exactly what
        # used to happen, every time an answer outlived the pass.
        #
        # The `finally` at the end runs no matter how we leave — finished,
        # failed, or you closed the tab — and its one job is to write down how
        # this question ended before the record is filed away.

    # Finding 30, and the whole of it is these three lines.
    #
    # A browser that disconnects mid-answer used to leave this generator
    # abandoned rather than closed — suspended at a `yield` forever — and three
    # things followed from that one cause: the answer was never saved, the trace
    # stayed "running" in the dashboard, and the model's stream stayed open and
    # billable until the garbage collector happened to reach it.
    #
    # So the generator is named instead of passed anonymously, and its `close`
    # is handed to the middleware that owns the end of this request.
    # `close_on_disconnect` calls it in a `finally`, which runs whether the
    # request ended normally or died — and closing raises `GeneratorExit` inside
    # `generate`, which is what finally lets its own `finally` block run.
    #
    # A `BackgroundTask` was the obvious alternative and does not work: read
    # from the installed Starlette, `StreamingResponse.__call__` raises
    # `ClientDisconnect` on the ASGI 2.4+ path and never reaches
    # `self.background()`. It would have fired on one code path and silently not
    # on the other.
    stream = generate()
    http_request.scope.get("state", {}).setdefault("stream_cleanup", []).append(
        stream.close
    )

    return StreamingResponse(
        stream,
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


if __name__ == "__main__":
    # Ours passes through whole: a status and a sentence written to be read by
    # the person who hit the error.
    ours = HTTPException(status_code=400, detail="No documents to search yet.")
    assert _safe_trace_error(ours) == "HTTPException 400: No documents to search yet."

    # A library's does not. The message is exactly the part that quotes the
    # failing request back at a page that may end up public — which is the whole
    # reason the Clerk id is hashed a few dozen lines above.
    class _PostgrestError(Exception):
        pass

    leaky = _PostgrestError(
        "relation chunks: SELECT embedding FROM chunks WHERE user_id='user_3ABCdef'"
    )
    rendered = _safe_trace_error(leaky)

    assert rendered == "_PostgrestError", rendered
    assert "user_3ABCdef" not in rendered, "a Clerk id reached a shareable trace"
    assert "SELECT" not in rendered, "a query reached a shareable trace"

    # Still says *something*. An empty error field would close the run without
    # recording that anything went wrong, which is the failure this whole
    # handler exists to prevent.
    assert rendered, "the trace must still record that it failed"

    # --- Finding 29: the right sentence for the right failure ----------------
    #
    # Order first, because it is the part that breaks silently. Both of these
    # subclass BadRequestError, so a mapping in the wrong order would answer
    # every one of them with the generic line and nothing would look wrong.
    # The provider's text is a sentinel, not plausible English. A realistic
    # message can share words with our own copy by coincidence, and then the
    # leak check below passes or fails for reasons that have nothing to do with
    # leaking — which is how a test starts lying.
    LEAK = "PROVIDER-DETAIL-b3f9"

    context = litellm.ContextWindowExceededError(
        message=LEAK, model="gpt-5.4-nano", llm_provider="openai"
    )
    _, detail = _stream_failure(context)
    assert "too long for the model" in detail, detail
    assert "Please try again" not in detail, (
        "an over-long conversation was told to retry, which cannot help"
    )

    rate_limited = litellm.RateLimitError(
        message=LEAK, model="m", llm_provider="openai"
    )
    _, busy = _stream_failure(rate_limited)
    assert "busy" in busy, busy

    bad_credentials = litellm.AuthenticationError(
        message=LEAK, model="m", llm_provider="openai"
    )
    _, bad_key = _stream_failure(bad_credentials)
    assert "our problem" in bad_key, bad_key

    # Nothing may quote the provider. These messages go to a browser, and a
    # provider's text can carry the request that produced it.
    for exc in (context, rate_limited, bad_credentials):
        _, shown = _stream_failure(exc)
        assert LEAK not in shown, shown

    # An unrecognised failure still says something.
    label, generic = _stream_failure(RuntimeError("something new"))
    assert generic == "The answer stopped early. Please try again."
    assert label == "Generation failed"
    assert "something new" not in generic

    # --- Finding 30: closing an abandoned generator runs its finally ---------
    #
    # The whole fix rests on two language facts. Assert them rather than trust
    # them, because if either is wrong the middleware runs and achieves nothing.
    cleaned: list[str] = []

    def _streamer():
        try:
            yield "first"
            yield "second"
        except Exception:
            # Must NOT catch the close. `generate()` has a handler of exactly
            # this shape, and if GeneratorExit were an Exception the disconnect
            # would be misreported as a provider failure.
            cleaned.append("wrongly caught")
            raise
        finally:
            cleaned.append("cleaned up")

    gen = _streamer()
    assert next(gen) == "first"
    gen.close()
    assert cleaned == ["cleaned up"], cleaned
    assert not issubclass(GeneratorExit, Exception), (
        "GeneratorExit is an Exception here — `except Exception` in generate() "
        "would swallow the close and report a disconnect as a provider failure"
    )

    # Closing twice, and closing something already finished, must both be safe:
    # the middleware cannot know which happened.
    gen.close()
    done = _streamer()
    list(done)
    done.close()

    print(
        "OK - trace errors: ours pass through whole, libraries reduced to a class name; "
        "stream failures mapped by cause; closing an abandoned generator runs its finally"
    )
