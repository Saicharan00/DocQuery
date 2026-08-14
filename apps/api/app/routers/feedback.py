"""Was that answer any good?

One endpoint. It takes a thumb and an optional sentence and writes them onto the
LangSmith trace of the answer being rated, which is the only place they are worth
keeping: a score on its own tells you the answer was bad, while a score attached
to its own trace tells you the retrieval scored 0.18 and four of the five chunks
were images.

Nothing is written to Postgres, so there is no row here for RLS to protect. The
endpoint still requires a verified token — an unauthenticated one would let
anybody push numbers into the dashboard the evaluation reads.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status

from app.deps import CurrentUser
from app.models.feedback import FeedbackRequest
from app.services import tracing

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/feedback", tags=["feedback"])


@router.post("", status_code=status.HTTP_204_NO_CONTENT)
def submit_feedback(_user_id: CurrentUser, request: FeedbackRequest) -> None:
    """Record one reader's verdict on one answer."""
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

    # ponytail: `run_id` is taken on trust. It is a UUIDv7 the caller can only
    # have got from their own answer's stream, and no row is written, so the worst
    # abuse is somebody re-rating an answer they already received. Storing the id
    # on the assistant message row would make it verifiable, at the cost of a
    # migration — worth it only if the ratings ever start informing a decision.
