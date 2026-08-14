"""What a reader is allowed to say about an answer.

Deliberately tiny. Feedback is not stored in Postgres — it goes onto the
LangSmith trace of the answer it is about, so the rating sits next to the
retrieval, the prompt and the timings that produced it. A separate table would
hold the same three fields with nothing to join them to.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class FeedbackRequest(BaseModel):
    """One verdict on one answer: a thumb, a sentence, or both."""

    # The trace of the answer being rated. The browser learns it from the
    # `conversation` event at the start of that answer's stream.
    run_id: UUID

    # 1 is a thumbs up, 0 a thumbs down. Integers rather than a bool because
    # LangSmith averages scores, and "0.62" is a readable answer to "how are the
    # answers doing"; `true`/`false` is not.
    #
    # Optional because the browser sends the thumb the instant it is clicked and
    # any comment afterwards, as a separate submission. Repeating the score on
    # that second call would count one reader's opinion twice in the average.
    score: Literal[0, 1] | None = None

    # Capped, and the ceiling is a spend brake in the same spirit as
    # `ChatRequest.message`: this text is uploaded to and stored by a third
    # party, and a paragraph is feedback where a pasted document is abuse.
    comment: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _must_say_something(self) -> "FeedbackRequest":
        if self.score is None and not (self.comment or "").strip():
            raise ValueError("Send a score, a comment, or both.")
        return self

    # In plain English: both fields are optional on their own, but a submission
    # with neither is meaningless, so the check above rejects it with a clear
    # reason rather than quietly filing an empty opinion.
