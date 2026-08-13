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
from uuid import UUID

from fastapi import APIRouter, HTTPException, status

from app.deps import CurrentUser, SupabaseClient
from app.models.conversation import ConversationOut, ConversationUpdate, MessageOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/conversations", tags=["conversations"])


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
        response = (
            supabase.table("conversations")
            .select("*")
            .order("updated_at", desc=True)
            .execute()
        )
    except Exception as exc:
        logger.exception("Listing conversations failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not read your conversations. Please try again.",
        ) from exc

    return [ConversationOut.model_validate(row) for row in response.data]

    # Sorted by `updated_at`, not `created_at`: a conversation you replied to an
    # hour ago belongs above one you started last week and abandoned.
    # `_save_exchange` in chat.py touches that column after every exchange for
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
        response = (
            supabase.table("messages")
            .select("*")
            .eq("conversation_id", str(conversation_id))
            # `created_at` alone is a genuine tie: both rows of an exchange are
            # written by one insert, and `now()` is the transaction clock, so the
            # question and its answer carry byte-identical timestamps. Ascending
            # time here, so the tie-break runs `desc` — 'user' sorts after
            # 'assistant', and descending puts it back on top where it belongs.
            .order("created_at")
            .order("role", desc=True)
            .execute()
        )
    except Exception as exc:
        logger.exception("Reading messages failed for conversation %s", conversation_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not read the conversation. Please try again.",
        ) from exc

    return [MessageOut.model_validate(row) for row in response.data]

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
