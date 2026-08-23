"""Reading, renaming and deleting past conversations.

Day 7 has been saving every exchange since it shipped; nothing could read them
back. These four endpoints are that missing half — the browser holds no database
credential and never will, so a row it cannot ask for is a row that does not
exist as far as the app is concerned.

As everywhere else in this API, not one query below carries a
`WHERE user_id = ...`. The `conversations_isolation` and `messages_isolation`
policies from `001_init.sql` scope every statement here to whoever holds the
token. See the RLS rule in CLAUDE.md.

Two things worth knowing before reading further.

**Every miss is a 404, never a 403.** RLS makes somebody else's conversation
indistinguishable from one that was never created — the select simply returns
nothing either way. Saying "not found" to both reveals less than confirming that
a conversation exists but belongs to someone else.

**Writes look before they leap.** `PATCH` and `DELETE` each run an existence
check first and act second. Not paranoia: PostgREST answers an RLS-rejected
write with an empty result set rather than an error (see `_resolve_conversation`
in `chat.py`), so a write whose result we cannot interpret is a rename that
silently does nothing. One indexed lookup on a primary key buys an unambiguous
answer, and it is the same two-step `delete_document` already uses.
"""

from __future__ import annotations

import logging
from typing import Any, Callable
from uuid import UUID

from fastapi import APIRouter, HTTPException, status

from app.deps import CurrentUser, SupabaseClient
from app.models.conversation import ConversationOut, ConversationUpdate, MessageOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/conversations", tags=["conversations"])

# PostgREST's own `db.max-rows` default (unrelated to any setting of ours) —
# a single response can never carry more than this many rows, and it still
# answers 200 when it clips one. Below, `.range()` is not an optimisation;
# without it every list here silently loses rows the moment a caller passes
# this many, and nothing about the response says so.
_PAGE_SIZE = 1000


def _select_all(query_factory: Callable[[], Any]) -> list[dict]:
    """Every row a query matches, fetched `_PAGE_SIZE` rows at a time.

    `query_factory` builds the query fresh on every call rather than one
    builder being reused with a different `.range()` each time — a
    `postgrest-py` request builder is meant to be executed once, not replayed.

    A page shorter than `_PAGE_SIZE` is how the end is recognised, the same
    way a `read()` loop stops on a short chunk — no separate count query
    needed to know when to stop.
    """
    rows: list[dict] = []
    offset = 0
    while True:
        page = query_factory().range(offset, offset + _PAGE_SIZE - 1).execute()
        rows.extend(page.data)
        if len(page.data) < _PAGE_SIZE:
            return rows
        offset += _PAGE_SIZE


def _require_owned(supabase, conversation_id: UUID) -> None:
    """404 unless this conversation exists and RLS lets the caller see it.

    The gate in front of every operation that names a specific conversation. It
    selects `id` and nothing else — the question is only whether a row comes
    back, and the answer is the same size whatever the row contains.
    """
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

    if not found.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found.",
        )


@router.get("", response_model=list[ConversationOut])
def list_conversations(
    _user_id: CurrentUser,
    supabase: SupabaseClient,
) -> list[ConversationOut]:
    """Every conversation belonging to the caller, most recently active first."""
    try:
        rows = _select_all(
            lambda: supabase.table("conversations")
            .select("*")
            .order("updated_at", desc=True)
        )
    except Exception as exc:
        logger.exception("Listing conversations failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not read your conversations. Please try again.",
        ) from exc

    return [ConversationOut.model_validate(row) for row in rows]

    # Sorted by `updated_at`, not `created_at`: a conversation you replied to an
    # hour ago belongs above one you started last week and abandoned.
    # `_save_answer` in chat.py touches that column after every exchange for
    # exactly this reason.


@router.get("/{conversation_id}/messages", response_model=list[MessageOut])
def list_messages(
    _user_id: CurrentUser,
    supabase: SupabaseClient,
    conversation_id: UUID,
) -> list[MessageOut]:
    """The whole conversation, oldest message first."""
    _require_owned(supabase, conversation_id)

    try:
        rows = _select_all(
            lambda: supabase.table("messages")
            .select("*")
            .eq("conversation_id", str(conversation_id))
            # `created_at` alone is a genuine tie: both rows of an exchange are
            # written by one insert, and `now()` is the transaction clock, so the
            # question and its answer carry byte-identical timestamps. Ascending
            # time here, so the tie-break runs `desc` — 'user' sorts after
            # 'assistant', and descending puts it back on top where it belongs.
            #
            # That same total order is what makes paging through this safely
            # possible at all: `_select_all` relies on two separate requests
            # agreeing on one exact ordering, which a tie left unbroken would
            # not guarantee.
            .order("created_at")
            .order("role", desc=True)
        )
    except Exception as exc:
        logger.exception("Reading messages failed for conversation %s", conversation_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not read the conversation. Please try again.",
        ) from exc

    return [MessageOut.model_validate(row) for row in rows]

    # The existence check above is not redundant with the query below. Without
    # it a deleted or unknown id would return an empty list, and the page would
    # render a blank conversation that looks broken rather than saying the
    # conversation is gone. Ascending order because a conversation is read from
    # the top; the default is ascending, but a reader should not have to know
    # that to know which way round the screen will be.


@router.patch("/{conversation_id}", response_model=ConversationOut)
def rename_conversation(
    _user_id: CurrentUser,
    supabase: SupabaseClient,
    conversation_id: UUID,
    update: ConversationUpdate,
) -> ConversationOut:
    """Give a conversation a new title.

    `updated_at` is deliberately left alone. It means "when was this last talked
    in", which is what the sidebar sorts by — renaming an old conversation must
    not shove it to the top as though it had new messages in it.
    """
    _require_owned(supabase, conversation_id)

    try:
        updated = (
            supabase.table("conversations")
            .update({"title": update.title})
            .eq("id", str(conversation_id))
            .execute()
        )
    except Exception as exc:
        logger.exception("Rename failed for conversation %s", conversation_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not rename the conversation. Please try again.",
        ) from exc

    if not updated.data:
        # The check above said the row is ours, so an empty result here is not
        # "not found" — it is the write itself being refused, or the client not
        # returning what it changed. Either way we cannot claim it worked.
        logger.error("Rename of conversation %s returned no row", conversation_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not rename the conversation. Please try again.",
        )

    return ConversationOut.model_validate(updated.data[0])


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation(
    _user_id: CurrentUser,
    supabase: SupabaseClient,
    conversation_id: UUID,
) -> None:
    """Delete a conversation and everything said in it.

    The messages need no code of their own: `messages.conversation_id` is
    declared `on delete cascade` in `001_init.sql`, so Postgres removes them as
    part of this statement. Unlike `delete_document`, nothing lives in Storage
    here, so there is no second system to keep in step.
    """
    _require_owned(supabase, conversation_id)

    try:
        supabase.table("conversations").delete().eq("id", str(conversation_id)).execute()
    except Exception as exc:
        logger.exception("Delete failed for conversation %s", conversation_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not delete the conversation. Please try again.",
        ) from exc


# In plain English: ask for the first 1000 rows. If exactly 1000 came back,
# that might mean there were more sitting behind them, so ask again for the
# next 1000, and keep doing that until a batch comes back with fewer than
# 1000 in it — that shortfall is what proves nothing was left behind. Before
# this, one request would quietly hand back only the first 1000 rows and say
# nothing about the rest.


# ---------------------------------------------------------------------------
# Self-check
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # _select_all, finding 25: reproduces PostgREST's own row cap with a fake
    # query object, no network or database needed.
    #   apps\api> .venv\Scripts\python.exe -m app.routers.conversations
    from types import SimpleNamespace

    def _fake_source(total: int):
        all_rows = [{"i": i} for i in range(total)]

        def factory():
            state: dict[str, int] = {}

            class FakeQuery:
                def range(self, start: int, end: int) -> "FakeQuery":
                    state["start"], state["end"] = start, end
                    return self

                def execute(self) -> SimpleNamespace:
                    # Capped at _PAGE_SIZE regardless of what was asked for —
                    # this is PostgREST's db.max-rows, not our own limit.
                    page = all_rows[state["start"] : state["end"] + 1][:_PAGE_SIZE]
                    return SimpleNamespace(data=page)

            return FakeQuery()

        return factory

    # Under one page: nothing to page through.
    assert len(_select_all(_fake_source(3))) == 3

    # Exactly the old truncation point — this is the case the original bug
    # got wrong, returning 1000 when the real answer was also 1000 (by luck)
    # or less (silently) whenever the true total exceeded it.
    assert len(_select_all(_fake_source(_PAGE_SIZE))) == _PAGE_SIZE

    # Spans three requests, the last one partial.
    total = _PAGE_SIZE * 2 + 7
    assert len(_select_all(_fake_source(total))) == total

    print("OK - _select_all pages past PostgREST's row cap instead of truncating")
