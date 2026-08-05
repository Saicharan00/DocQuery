"""Document CRUD.

Note what is deliberately absent from every query here: a `WHERE user_id = ...`
filter. Row-level security in `001_init.sql` does that, using the `sub` claim of
the Clerk token the Supabase client forwards. Filtering here as well would mask
whether RLS is actually doing its job — see the RLS rule in CLAUDE.md.

A document lives in two places at once: the bytes in Supabase Storage, the facts
in the `documents` table. `file_path` is the string tying them together, and the
write order in each handler below exists to keep them from disagreeing.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from uuid import UUID, uuid4

from cohere.errors import TooManyRequestsError
from fastapi import APIRouter, File, HTTPException, UploadFile, status

from app.config import get_settings
from app.deps import CurrentUser, SupabaseClient
from app.models.document import DocumentOut, IngestStepOut
from app.services import ingestion

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/documents", tags=["documents"])

BUCKET = "documents"

# The extension is the gate, and it also picks the MIME type we record. We
# never store the browser's declared content type: it is client-supplied and
# can be wrong or forged, so it is not something to persist and trust later.
ALLOWED_EXTENSIONS: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
}

MAX_FILE_BYTES = 10 * 1024 * 1024
READ_CHUNK_BYTES = 1024 * 1024

# How long one ingestion step is allowed to work before returning and asking the
# browser to call again. A Clerk session token lives about 60 seconds, and every
# write here depends on that token being valid — so a step has to finish well
# inside one token's life. The remaining ~15s covers the round trip and the final
# status write.
STEP_BUDGET_SECONDS = 45


@router.get("", response_model=list[DocumentOut])
async def list_documents(
    _user_id: CurrentUser,
    supabase: SupabaseClient,
) -> list[DocumentOut]:
    """Every document belonging to the caller, newest first."""
    try:
        response = (
            supabase.table("documents")
            .select("*")
            .order("created_at", desc=True)
            .execute()
        )
    except Exception as exc:
        # Most likely cause is the Clerk -> Supabase trust configuration: if
        # Supabase doesn't accept the token, `request.jwt.claims` is empty and
        # the request never reaches the RLS policy as an authenticated user.
        logger.exception("Listing documents failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not read your documents. Please try again.",
        ) from exc

    return [DocumentOut.model_validate(row) for row in response.data]


def _enforce_daily_limit(supabase) -> None:
    """Refuse an upload once the caller has had enough for one day.

    Every chunk of every document is a billed embedding call, paid from my own
    Cohere key — there is no BYOK any more, so this cap protects a card rather
    than a free quota. Checked before the file is read, so a rejected upload
    doesn't cost us 10MB of memory first.

    Two limits, checked in order. The global one first: if the whole service has
    spent enough for one day, whose turn it is stops mattering. It reads a count
    RLS would otherwise hide, via the `security definer` function in migration
    004 — the API still holds no key that can bypass RLS for writes.

    The per-user count needs no `user_id` filter, as everywhere else in this
    file: RLS scopes it to the caller.

    ponytail: deleting a document frees an allowance, so a determined user can
    exceed the cap. Accepted — the cap is a spend brake, not a security control.
    """
    settings = get_settings()

    try:
        today = supabase.rpc("documents_created_today").execute()
    except Exception as exc:
        logger.exception("Global limit check failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not check service capacity. Please try again.",
        ) from exc

    if (today.data or 0) >= settings.global_daily_document_limit:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="This demo has hit its daily limit. Please try again tomorrow.",
        )

    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

    try:
        recent = (
            supabase.table("documents")
            .select("id", count="exact")
            .gte("created_at", since)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        logger.exception("Daily limit check failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not check your upload allowance. Please try again.",
        ) from exc

    limit = settings.max_documents_per_day
    if (recent.count or 0) >= limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Daily upload limit reached ({limit} documents in 24 hours). Try again tomorrow.",
        )


async def _read_within_limit(file: UploadFile) -> bytes:
    """Buffer the upload, refusing to hold more than `MAX_FILE_BYTES`.

    Read in chunks rather than one `await file.read()` so an oversized upload
    stops costing us memory the moment it crosses the limit, instead of being
    buffered in full and only then rejected.
    """
    data = bytearray()
    while chunk := await file.read(READ_CHUNK_BYTES):
        data.extend(chunk)
        if len(data) > MAX_FILE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="File is too large. The limit is 10MB.",
            )
    return bytes(data)


@router.post("/upload", response_model=DocumentOut, status_code=status.HTTP_201_CREATED)
async def upload_document(
    user_id: CurrentUser,
    supabase: SupabaseClient,
    file: UploadFile = File(...),
) -> DocumentOut:
    """Store an uploaded file and record it.

    Bytes go to Storage before the row is inserted. The reverse order would let
    a row exist that points at a file that was never written — a document that
    looks fine in the UI and then fails ingestion. If the insert fails we delete
    the object we just wrote, so a failure leaves nothing behind either way.
    """
    # `filename` is client-supplied: it can be absent, and it can contain path
    # separators. Take the basename so nothing can steer the storage key.
    _enforce_daily_limit(supabase)

    raw_name = PurePosixPath((file.filename or "").replace("\\", "/")).name
    extension = PurePosixPath(raw_name).suffix.lower()

    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported file type. Upload a PDF, DOCX, or TXT file.",
        )

    contents = await _read_within_limit(file)
    if not contents:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That file is empty.",
        )

    # Generated here, not by the database, because the storage key needs it
    # before any row exists.
    document_id = uuid4()

    # The `{user_id}/` prefix is what the storage policy in 001_init.sql checks
    # via `storage.foldername(name)[1]`. Storage RLS rejects any other prefix.
    file_path = f"{user_id}/{document_id}{extension}"
    mime_type = ALLOWED_EXTENSIONS[extension]

    bucket = supabase.storage.from_(BUCKET)

    try:
        bucket.upload(
            path=file_path,
            file=contents,
            file_options={"content-type": mime_type, "upsert": "false"},
        )
    except Exception as exc:
        logger.exception("Storage upload failed for %s", file_path)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not store the file. Please try again.",
        ) from exc

    row = {
        "id": str(document_id),
        # Sent explicitly because the column has no default. RLS re-checks it
        # against the token's `sub`, so this cannot be used to write a row
        # belonging to somebody else.
        "user_id": user_id,
        "name": raw_name,
        "file_path": file_path,
        "status": "pending",
        "file_size": len(contents),
        "mime_type": mime_type,
    }

    try:
        response = supabase.table("documents").insert(row).execute()
    except Exception as exc:
        logger.exception("Insert failed for %s; removing the uploaded object", file_path)
        _remove_object(bucket, file_path)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not save the document. Please try again.",
        ) from exc

    if not response.data:
        # PostgREST returns an empty set rather than an error when an RLS
        # WITH CHECK rejects the row.
        logger.error("Insert returned no row for %s; removing the object", file_path)
        _remove_object(bucket, file_path)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not save the document. Please try again.",
        )

    return DocumentOut.model_validate(response.data[0])


def _remove_object(bucket, file_path: str) -> None:
    """Best-effort cleanup of an object whose row could not be written.

    Deliberately swallows its own failure: the caller is already returning an
    error, and this is compensation for that error rather than the operation
    the user asked for. It is logged so an orphan can be found later.
    """
    try:
        bucket.remove([file_path])
    except Exception:
        logger.exception("Orphaned storage object left behind at %s", file_path)


def _set_status(
    supabase,
    document_id: UUID,
    new_status: str,
    error: str | None = None,
) -> None:
    """Move a document to a status, recording (or clearing) its error."""
    supabase.table("documents").update({"status": new_status, "error": error}).eq(
        "id", str(document_id)
    ).execute()


# Deliberately `def`, not `async def`. Embedding is a blocking network call
# repeated for up to 45 seconds; inside an `async def` that would freeze the
# entire API for every other user while one document ingests. FastAPI runs a
# sync handler in a worker thread, so the event loop stays free.
@router.post("/{document_id}/ingest/step", response_model=IngestStepOut)
def ingest_step(
    user_id: CurrentUser,
    supabase: SupabaseClient,
    document_id: UUID,
) -> IngestStepOut:
    """Do one slice of ingestion, then hand control back to the browser.

    Ingestion cannot run as a background job here: the job would outlive the
    Clerk token it needs, and that token is what makes RLS work. So the browser
    drives it instead — each call is an ordinary authenticated request that works
    for `STEP_BUDGET_SECONDS`, writes what it finished, and reports where it got
    to. The browser calls again with a fresh token until `done` is true.

    Two consequences worth knowing. Closing the tab mid-way is recoverable: the
    document sits at `processing` with its finished chunks saved, and the next
    call resumes from there. And re-calling this on a `failed` document retries
    it, keeping whatever it managed to write the first time.
    """
    try:
        found = (
            supabase.table("documents")
            .select("file_path, mime_type, status")
            .eq("id", str(document_id))
            .execute()
        )
    except Exception as exc:
        logger.exception("Lookup failed for document %s", document_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not start processing. Please try again.",
        ) from exc

    # RLS scopes this to the caller, so somebody else's id is indistinguishable
    # from one that doesn't exist — the same 404 as `delete_document`.
    if not found.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found.",
        )

    document = found.data[0]

    if document["status"] == "ready":
        # A duplicated or late call must not re-embed a finished document. It is
        # billable work, so this returns rather than repeating it.
        total = _count_chunks(supabase, document_id)
        # No page figures here: reporting them would mean re-downloading and
        # re-parsing a finished document just to draw a bar that is already full.
        return IngestStepOut(
            done=True,
            chunks_done=total,
            chunks_total=total,
            status="ready",
            page=0,
            pages=0,
        )

    try:
        _set_status(supabase, document_id, "processing")

        # The resume point. Chunks are written in index order, so the highest
        # index present is where the last step stopped — and `chunk()` being
        # deterministic is what makes that number mean the same thing now as it
        # did then.
        highest = (
            supabase.table("chunks")
            .select("chunk_index")
            .eq("document_id", str(document_id))
            .order("chunk_index", desc=True)
            .limit(1)
            .execute()
        )
        resume_from = highest.data[0]["chunk_index"] + 1 if highest.data else 0

        data = ingestion.download(supabase, document["file_path"])
        chunks = ingestion.chunk(ingestion.parse(data, document["mime_type"]))
        total = len(chunks)
        remaining = chunks[resume_from:]

        started = time.monotonic()
        written = resume_from

        for offset in range(0, len(remaining), ingestion.EMBED_BATCH_SIZE):
            batch = remaining[offset : offset + ingestion.EMBED_BATCH_SIZE]

            try:
                vectors = ingestion.embed([item.content for item in batch])
            except TooManyRequestsError:
                # Not a failure — the embedding provider is saying "slower".
                # End the step with whatever is already written and let the
                # browser come back. Treating this as fatal would mark a
                # perfectly good document `failed` for a condition that clears
                # itself in under a minute.
                logger.warning(
                    "Rate limited by the embedding provider at chunk %s of %s",
                    written,
                    total,
                )
                break

            supabase.table("chunks").insert(
                [
                    {
                        # From the verified token, not from the document row.
                        # RLS checks this value against the token anyway, and
                        # copying it from the row would be a habit that turns
                        # dangerous on any path RLS doesn't cover.
                        "user_id": user_id,
                        "document_id": str(document_id),
                        "content": item.content,
                        "embedding": vector,
                        "chunk_index": item.index,
                        "token_count": item.token_count,
                        "page_number": item.page_number,
                        "chunk_type": "text",
                    }
                    for item, vector in zip(batch, vectors)
                ]
            ).execute()

            written += len(batch)

            # Checked after a batch, never before: one batch always completes, so
            # a step can never return having done nothing. A step that made no
            # progress would leave the browser looping forever.
            #
            # The very first step returns after one batch instead of working the
            # full budget. Nothing can be drawn until a step comes back with the
            # totals, and spending 45 seconds before revealing them leaves the
            # page looking frozen. One extra round trip buys immediate feedback.
            if resume_from == 0 or time.monotonic() - started > STEP_BUDGET_SECONDS:
                break

        done = written >= total
        if done:
            _set_status(supabase, document_id, "ready")

        return IngestStepOut(
            done=done,
            chunks_done=written,
            chunks_total=total,
            status="ready" if done else "processing",
            # `written` counts chunks, so the last one stored is at index
            # `written - 1`. Guarded because a rate limit can end a step before
            # anything was written at all.
            page=chunks[written - 1].page_number if written else 0,
            pages=max((item.page_number for item in chunks), default=0),
        )

    except Exception as exc:
        # The message is stored on the row and shown to the user, so it has to
        # say something real — "No readable text found…" from `parse`, for
        # instance. Chunks already written stay: a retry resumes from them.
        logger.exception("Ingestion step failed for document %s", document_id)
        message = str(exc) or exc.__class__.__name__
        try:
            _set_status(supabase, document_id, "failed", message[:500])
        except Exception:
            logger.exception("Could not record the failure on document %s", document_id)

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=message[:500],
        ) from exc


def _count_chunks(supabase, document_id: UUID) -> int:
    response = (
        supabase.table("chunks")
        .select("id", count="exact")
        .eq("document_id", str(document_id))
        .limit(1)
        .execute()
    )
    return response.count or 0


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    _user_id: CurrentUser,
    supabase: SupabaseClient,
    document_id: UUID,
) -> None:
    """Delete a document's file and its row.

    Storage object first, row second. If storage deletion fails, the row
    survives and you can retry. The other order would leave a file with nothing
    pointing at it — unfindable.

    Day 6's chunks disappear on their own: `chunks.document_id` is declared
    `on delete cascade` in 001_init.sql.
    """
    try:
        found = (
            supabase.table("documents")
            .select("file_path")
            .eq("id", str(document_id))
            .execute()
        )
    except Exception as exc:
        logger.exception("Lookup failed for document %s", document_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not delete the document. Please try again.",
        ) from exc

    # RLS scopes this select to the caller, so somebody else's id returns
    # nothing and is indistinguishable from an id that does not exist. That is
    # the intent: a 404 reveals less than a 403.
    if not found.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found.",
        )

    file_path = found.data[0]["file_path"]

    try:
        supabase.storage.from_(BUCKET).remove([file_path])
    except Exception as exc:
        logger.exception("Storage delete failed for %s", file_path)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not delete the file. Please try again.",
        ) from exc

    try:
        supabase.table("documents").delete().eq("id", str(document_id)).execute()
    except Exception as exc:
        logger.exception("Row delete failed for document %s", document_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The file was removed but the record could not be. Please try again.",
        ) from exc
