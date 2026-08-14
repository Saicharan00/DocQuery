"""Was that answer any good?

One endpoint. It takes a thumb and an optional sentence and writes them onto the
LangSmith trace of the answer being rated, which is the only place they are worth
keeping: a score on its own tells you the answer was bad, while a score attached
to its own trace tells you the retrieval scored 0.18 and four of the five chunks
were images.

Nothing is written to Postgres, but the caller does not get to name any run they
like. Since migration 006 the answer's run id is stored on its `messages` row, so
the rating is checked against a row RLS will only show to its owner — the same
boundary every other read in this app leans on, rather than a `where user_id`
this file would have to remember to write.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status

from app.deps import CurrentUser, SupabaseClient
from app.models.feedback import FeedbackRequest
from app.services import tracing

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/feedback", tags=["feedback"])


@router.post("", status_code=status.HTTP_204_NO_CONTENT)
def submit_feedback(
    _user_id: CurrentUser,
    supabase: SupabaseClient,
    request: FeedbackRequest,
) -> None:
    """Record one reader's verdict on one answer."""
    try:
        owned = (
            supabase.table("messages")
            .select("id")
            .eq("run_id", str(request.run_id))
            .limit(1)
            .execute()
        )
    except Exception as exc:
        logger.exception("Could not check ownership of run %s", request.run_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not record your feedback. Please try again.",
        ) from exc

    # No `where user_id = ...` here, deliberately: `messages_isolation` from
    # 001_init.sql already scopes this select to the caller, so somebody else's
    # answer is indistinguishable from one that does not exist. Same 404 either
    # way, which reveals less than a 403 would.
    if not owned.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="That answer is no longer available to rate.",
        )

    # In plain English: before filing an opinion, check the answer being rated is
    # one this person actually received. The database only shows them their own
    # messages, so finding no row means the run is either someone else's or made
    # up — and both get the same reply, which tells a prober nothing.

    if tracing.record_feedback(str(request.run_id), request.score, request.comment):
        return

    # Reached two ways: tracing is switched off, or the upload failed. The first
    # should be unreachable from the UI — with tracing off the stream sends no
    # run id and the buttons never render — so treat both as a failure the reader
    # deserves to know about. They clicked something and it did not happen;
    # silently swallowing that is how a feedback box becomes decorative.
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="Could not record your feedback. Please try again.",
    )
