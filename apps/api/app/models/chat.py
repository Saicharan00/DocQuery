"""What a chat request is allowed to be.

One model in this file, and its job is spending control rather than tidiness.
Every field is a limit on what a caller can make the server do with my money:
how long a question may be, which model may answer it, and which conversation it
may join.

The response has no model here on purpose — `/chat` streams SSE, so there is no
single JSON body for FastAPI to validate on the way out. The shape of a citation
lives with the code that builds it, in `rag.to_sources`, where the numbering it
has to agree with is visible on the same screen.
"""

from __future__ import annotations

from typing import Literal, get_args
from uuid import UUID

from pydantic import BaseModel, Field

from app.services import rag

# THE ALLOWLIST. Written out literally rather than derived from
# `rag.SUPPORTED_MODELS`, because a `Literal` has to be a static type for
# FastAPI to reject an unknown model before the handler runs — and being
# rejected by validation is the point. Without this a caller names any model
# they like, LiteLLM cheerfully routes to it, and the bill is mine.
ModelName = Literal["gemini/gemini-3.5-flash-lite", "gpt-5.4-nano"]

# Two lists that must agree, so they are compared once at import. A mismatch
# stops the app at startup instead of surfacing as a confusing 503 on somebody's
# question. Not the security boundary — `rag.api_key_for` still refuses a model
# it has no key for — just the earliest possible warning that they drifted.
assert set(get_args(ModelName)) == set(rag.SUPPORTED_MODELS), (
    "ModelName and rag.SUPPORTED_MODELS have drifted apart"
)


class ChatRequest(BaseModel):
    """One question, optionally continuing an existing conversation."""

    # Null means "start a new one". The endpoint creates it and reports the new
    # id in the first SSE event, so the browser can send it back on the next
    # question without a second round trip.
    conversation_id: UUID | None = None

    # The floor stops an empty question costing an embedding call. The ceiling
    # is a spend brake: 2000 characters is a generous question and a cheap one,
    # where a pasted 50-page document is neither.
    message: str = Field(min_length=1, max_length=2000)

    model: ModelName = rag.DEFAULT_MODEL

    # In plain English: `Field(min_length=1, max_length=2000)` tells Pydantic to
    # check the text's length itself and return a 422 with a clear reason if it
    # fails — before any of our code runs, so an oversized question never
    # reaches Cohere. `ModelName` does the same job for the model name: anything
    # not in that list of two is refused the same way.
