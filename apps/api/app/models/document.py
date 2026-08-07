"""Response shapes for the documents API.

Pydantic models here pin down what leaves the API. Returning raw Supabase rows
would let a schema change silently alter the JSON the frontend depends on.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

DocumentStatus = Literal["pending", "processing", "ready", "failed"]


class DocumentOut(BaseModel):
    """A document as the frontend sees it.

    `file_path` is included on purpose — it's the Storage key, and Day 6's
    ingestion needs it to fetch the file back. It's not a secret: the path is
    prefixed with the owner's user id, and Storage RLS enforces that prefix.
    """

    id: UUID
    name: str
    file_path: str
    status: DocumentStatus
    file_size: int | None = None
    mime_type: str | None = None
    error: str | None = None
    created_at: datetime


class IngestStepOut(BaseModel):
    """The result of one ingestion step.

    `done` is what the browser loops on: false means call again with a fresh
    token. The two counts exist so the UI can show progress without a second
    request — the step already knows them.
    """

    done: bool
    chunks_done: int
    chunks_total: int
    status: DocumentStatus
    # Which page the last stored chunk came from, and how many the document has.
    # Both come from `chunks.page_number`, which exists for citations later —
    # this reuses it to show what is being read right now.
    page: int
    pages: int
    # How many of `chunks_total` are pictures rather than text. Without it the
    # only evidence that image ingestion ran at all is a SQL query.
    images_total: int = 0
