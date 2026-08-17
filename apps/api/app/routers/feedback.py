"""Was that answer any good? And is DocQuery any good?

Two endpoints, and they store their answers in different places because they are
different questions.

`POST /feedback` judges one answer, and writes onto the LangSmith trace of that
answer. A score on its own tells you the answer was bad; a score attached to its
own trace tells you the retrieval scored 0.18 and four of the five chunks were
images. Nothing goes to Postgres, but the caller does not get to name any run
they like: since migration 006 the answer's run id is stored on its `messages`
row, so the rating is checked against a row RLS will only show to its owner —
the same boundary every other read in this app leans on, rather than a
`where user_id` this file would have to remember to write.

`POST /feedback/product` judges the whole thing, and has no run to attach to, so
it writes a row to `product_feedback` (migration 007) instead.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status

from app.deps import CurrentUser, SupabaseClient
from app.models.feedback import FeedbackRequest, ProductFeedbackRequest
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

    failed = tracing.record_feedback(str(request.run_id), request.score, request.comment)

    if not failed:
        return

    # Reached two ways: tracing is switched off, or the upload failed. The first
    # should be unreachable from the UI — with tracing off the stream sends no
    # run id and the buttons never render — so treat both as a failure the reader
    # deserves to know about. They clicked something and it did not happen;
    # silently swallowing that is how a feedback box becomes decorative.
    #
    # Naming the parts matters when only some of them landed. "Please try again"
    # against a half-stored submission is advice that corrupts the data it is
    # trying to save: the rating is already filed, and following the advice files
    # it a second time. Our own UI cannot reach that case — it posts the thumb
    # and the note as two separate requests — but the endpoint accepts both in
    # one body, so anything calling it directly can.
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=(
            f"Your {' and '.join(failed)} could not be recorded. "
            "Please try again — anything not named here was saved."
        ),
    )


@router.post("/product", status_code=status.HTTP_204_NO_CONTENT)
def submit_product_feedback(
    user_id: CurrentUser,
    supabase: SupabaseClient,
    request: ProductFeedbackRequest,
) -> None:
    """Record one reader's verdict on DocQuery as a whole."""
    # `user_id` is written explicitly here, unlike every read in this app, and
    # that is not a retreat from the RLS rule. A read is *filtered* by the
    # policy; an insert has to supply the column the policy then checks. Sending
    # somebody else's id is not a way in — `product_feedback_isolation` rejects
    # the row — but the column cannot be left for the database to guess.
    try:
        supabase.table("product_feedback").insert(
            {
                "user_id": user_id,
                "rating": request.rating,
                "comment": request.comment,
            }
        ).execute()
    except Exception as exc:
        # `exception` rather than `error` so the traceback reaches the log, and
        # nothing from `exc` reaches the reader: a database client's message can
        # carry the statement it tried to run.
        logger.exception("Could not store product feedback")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not record your feedback. Please try again.",
        ) from exc

    # In plain English: write one row holding who said it, how many stars they
    # gave, and anything they typed. If the database refuses for any reason, say
    # so plainly instead of returning a success the reader would believe — the
    # detail of *why* stays in the server log, where it is useful and safe.
