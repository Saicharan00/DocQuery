"""What a reader is allowed to say — about one answer, and about the product.

Two requests, and the difference between them is where they can possibly be
stored. `FeedbackRequest` judges one answer, so it goes onto that answer's
LangSmith trace and never touches Postgres: the rating is only actionable next
to the retrieval, the prompt and the timings that produced it.

`ProductFeedbackRequest` judges DocQuery as a whole. There is no run behind
"is this any good", so there is nothing to attach it to and it goes in the
`product_feedback` table from migration 007 instead.
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


class ProductFeedbackRequest(BaseModel):
    """One reader's verdict on DocQuery itself."""

    # No run id, and that absence is the whole reason this model exists rather
    # than reusing the one above. This opinion is not about anything the
    # pipeline produced, so there is no trace it could be filed against.
    #
    # 1-5 rather than a thumb: a product has degrees ("usable but slow") that a
    # single answer does not, and a mean of stars stays readable where a mean of
    # thumbs flattens everything into one number.
    rating: Literal[1, 2, 3, 4, 5] | None = None

    # Roomier than the per-answer ceiling of 1000. This one goes into our own
    # database rather than being uploaded to a third party, and someone who
    # takes the trouble to write a paragraph about the product is exactly the
    # feedback worth having in full.
    comment: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _must_say_something(self) -> "ProductFeedbackRequest":
        if self.rating is None and not (self.comment or "").strip():
            raise ValueError("Send a rating, a comment, or both.")
        return self
