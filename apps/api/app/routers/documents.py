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
from app.deps import CurrentUser, SupabaseClient, TokenExpiry
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

# The longest one ingestion step may work before returning and asking the browser
# to call again. It is a ceiling, not the budget: the real budget is whichever is
# smaller, this or the time left on the caller's token (see `ingest_step`).
STEP_BUDGET_SECONDS = 45

# Subtracted from the token's remaining life so a step stops before its
# credential expires rather than exactly as it does. Covers the last database
# write, the status update, and any clock difference between us and Supabase.
TOKEN_SAFETY_MARGIN_SECONDS = 5


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


def _items(data: bytes, mime_type: str | None) -> list[ingestion.Chunk]:
    """Everything to be embedded for this document, text first, images after.

    The order is load-bearing, not cosmetic. A step resumes from the highest
    `chunk_index` already stored, so images must continue the text's numbering
    and never sit among it: appending them means a resumed step never
    re-renders a picture it already stored, and `max(chunk_index)` keeps meaning
    exactly what it meant on Day 6a.

    Both halves are deterministic — `chunk` by construction, `find_images` by
    sorting boxes into reading order — which is what makes an index refer to the
    same thing on every call.

    ponytail: every step re-parses and re-renders the whole document, including
    the pictures it already stored. Measured at 0.6s for a 12-image report,
    against a 45s budget. Skip the rendering of anything below `resume_from` if
    a document ever makes that hurt.
    """
    chunks = ingestion.chunk(ingestion.parse(data, mime_type))

    return chunks + [
        ingestion.Chunk(
            index=len(chunks) + offset,
            # A label, not the thing being embedded. `chunks.content` is
            # `not null`, and Day 8 can show this next to a picture.
            content=f"[Image from page {region.page_number}]",
            page_number=region.page_number,
            token_count=0,
            image=region.jpeg,
        )
        for offset, region in enumerate(ingestion.find_images(data, mime_type))
    ]


def _batches(items: list[ingestion.Chunk]):
    """Split into runs of one kind, each no larger than that kind's batch size.

    Text and pictures cannot travel in the same Cohere call — `embed(texts=…)`
    and `embed(images=…)` are separate requests — so a batch that straddled the
    boundary would be impossible to send. Images also batch far smaller: 96
    JPEGs would blow past the 20MB-per-request ceiling.
    """
    start = 0

    while start < len(items):
        is_image = items[start].image is not None
        size = (
            ingestion.IMAGE_EMBED_BATCH_SIZE if is_image else ingestion.EMBED_BATCH_SIZE
        )
        end = start

        while (
            end < len(items)
            and end - start < size
            and (items[end].image is not None) == is_image
        ):
            end += 1

        yield items[start:end]
        start = end


def _token_expired(exc: Exception) -> bool:
    """True when Supabase refused a call because the caller's JWT had expired.

    This belongs in the same category as a rate limit, not in the same category
    as a corrupt file: it is temporary, and the next step arrives with a freshly
    minted token. Treating it as fatal marks a perfectly good document `failed`
    for a condition that fixes itself in seconds.

    Matched on the message because Storage and PostgREST raise different
    exception types for the identical underlying condition, and neither exposes
    the reason as a field. Deliberately narrow: a looser match would swallow a
    genuine authorisation failure and stall a document that should have failed
    loudly.
    """
    text = str(exc)
    return '"exp" claim' in text or "JWT expired" in text


# Deliberately `def`, not `async def`. Embedding is a blocking network call
# repeated for up to 45 seconds; inside an `async def` that would freeze the
# entire API for every other user while one document ingests. FastAPI runs a
# sync handler in a worker thread, so the event loop stays free.
@router.post("/{document_id}/ingest/step", response_model=IngestStepOut)
def ingest_step(
    user_id: CurrentUser,
    supabase: SupabaseClient,
    expires_at: TokenExpiry,
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
    # Started here rather than at the embedding loop so the budget covers the
    # whole request — the download and parse happen on the same token's clock.
    started = time.monotonic()

    # The real budget: never longer than the ceiling, and never past the moment
    # this request's own token dies. Clerk caches tokens and refreshes them only
    # when they are nearly spent, so a step can arrive holding far less than a
    # full token's life; working to a fixed 45s got a write rejected mid-step
    # with `"exp" claim timestamp check failed`. `expires_at` is wall-clock, so
    # it is compared against `time.time()` once, here — everything after this
    # measures elapsed time with the monotonic clock, which cannot jump.
    budget = min(
        STEP_BUDGET_SECONDS,
        expires_at - time.time() - TOKEN_SAFETY_MARGIN_SECONDS,
    )

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
        mime_type = document["mime_type"]
        chunks = _items(data, mime_type)

        if not chunks:
            # Neither text nor pictures. Only here can that be judged: a scanned
            # PDF has no text either, and it is now perfectly ingestible.
            raise ValueError(
                "Nothing could be read from this file — no text and no images."
            )

        total = len(chunks)
        images_total = sum(1 for item in chunks if item.image)
        remaining = chunks[resume_from:]

        bucket = supabase.storage.from_(BUCKET)
        written = resume_from

        for batch in _batches(remaining):
            is_image = batch[0].image is not None

            try:
                vectors = (
                    ingestion.embed_images([item.image for item in batch])
                    if is_image
                    else ingestion.embed([item.content for item in batch])
                )
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

            rows = []

            try:
                for item, vector in zip(batch, vectors):
                    image_path = None

                    if item.image:
                        # File first, row second — the same order as
                        # `upload_document`, for the same reason: a row pointing
                        # at a file that was never written is a broken document
                        # that looks fine. The path is derived from the chunk
                        # index rather than being random, so a replayed step
                        # overwrites its own file instead of leaving a duplicate.
                        # `foldername(...)[1]` is still the user id, so the
                        # storage policy from 001_init.sql passes unchanged.
                        image_path = f"{user_id}/{document_id}/img-{item.index}.jpg"
                        bucket.upload(
                            path=image_path,
                            file=item.image,
                            file_options={
                                "content-type": "image/jpeg",
                                "upsert": "true",
                            },
                        )

                    rows.append(
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
                            "chunk_type": "image" if item.image else "text",
                            "image_path": image_path,
                        }
                    )

                supabase.table("chunks").insert(rows).execute()
            except Exception as exc:
                if not _token_expired(exc):
                    raise
                # The budget above should prevent this, but clock skew and a
                # slow final batch can still get us here. Same treatment as a
                # rate limit: keep what is written, stay `processing`, and let
                # the browser return with a fresh token.
                logger.warning(
                    "Token expired mid-step at chunk %s of %s", written, total
                )
                break

            written += len(batch)

            # Checked after a batch, never before: one batch always completes, so
            # a step can never return having done nothing. A step that made no
            # progress would leave the browser looping forever.
            #
            # The very first step returns after one batch instead of working the
            # full budget. Nothing can be drawn until a step comes back with the
            # totals, and spending 45 seconds before revealing them leaves the
            # page looking frozen. One extra round trip buys immediate feedback.
            if resume_from == 0 or time.monotonic() - started > budget:
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
            images_total=images_total,
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

    # The images Day 6b cropped out of this document. They are separate Storage
    # objects with nothing cascading to them, so without this every delete
    # leaves its pictures in the bucket forever — paid for, and listed nowhere.
    # Bounded by MAX_IMAGES_PER_DOCUMENT, and RLS scopes the select as always.
    #
    # ponytail: this finds the images that made it into a row. A step that died
    # between uploading a JPEG and inserting its row leaves that one file
    # behind. Listing the folder prefix would catch those too — revisit if
    # orphans ever actually show up.
    try:
        stored = (
            supabase.table("chunks")
            .select("image_path")
            .eq("document_id", str(document_id))
            .not_.is_("image_path", "null")
            .execute()
        )
    except Exception as exc:
        logger.exception("Could not list image files for document %s", document_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not delete the document. Please try again.",
        ) from exc

    paths = [file_path] + [row["image_path"] for row in stored.data]

    try:
        supabase.storage.from_(BUCKET).remove(paths)
    except Exception as exc:
        logger.exception("Storage delete failed for %s (%s objects)", file_path, len(paths))
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
