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
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Annotated, Iterator
from uuid import UUID, uuid4

import httpx
from cohere.errors import (
    GatewayTimeoutError,
    InternalServerError,
    ServiceUnavailableError,
    TooManyRequestsError,
)
from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile, status

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

# Below this much usable budget, refuse the step before spending anything. One
# step must have room to download the file, parse it (rendering every figure),
# embed a batch and write it — do less than all of that and the money is spent
# for nothing, because a step that saves no chunks leaves the browser to repeat
# it.
#
# The twin of `MIN_ANSWER_BUDGET_SECONDS` in chat.py, and it carries the same
# warning: it must stay well below `STEP_BUDGET_SECONDS`. Clerk's session tokens
# live 60 seconds, so a floor set anywhere near the 45s ceiling would reject a
# brand-new token as readily as a dying one, and ingestion would 401 forever.
MIN_STEP_BUDGET_SECONDS = 20


# Which documents currently have a step running. `ingest_step` had a `ready`
# check but nothing stopping two of them overlapping, and two steps read the
# same resume point, **both pay Cohere for the identical batch**, and then one
# insert wins while the other violates `chunks_document_chunk_idx` and lands in
# the generic handler — marking a perfectly good document `failed`. On the worse
# ordering the loser overwrites `ready` with `failed` on a document that had
# just finished.
#
# Two tabs trigger it, and so does leaving the dashboard and coming back, since
# the browser-side guard is a per-mount ref.
#
# A `threading.Lock`, not an `asyncio` one: `ingest_step` is deliberately a sync
# `def`, so FastAPI runs it in a worker thread and there is no event loop here
# to await on.
#
# ponytail: process-local. Two Railway instances would each keep their own set
# and neither would see the other's claim. Upgrade path when a second instance
# exists: a `lease_expires_at` column on `documents`, claimed with a conditional
# UPDATE — the same idea, moved into the one place both processes share.
_steps_in_flight: set[str] = set()
_in_flight_lock = threading.Lock()


def claim_ingest_step(_user_id: CurrentUser, document_id: UUID) -> Iterator[None]:
    """Take the only ticket to ingest this document, or refuse the request.

    A dependency rather than a block inside the handler, for one reason that
    matters: this way the claim is held across the *whole* request, including
    the `status == "ready"` check near the top. A guard that started lower down
    would let a second caller read `processing`, wait, and then set a finished
    document back to `processing` — the exact overwrite this is here to stop.

    Depends on `CurrentUser` so an unauthenticated request is rejected before it
    can touch the set at all.
    """
    key = str(document_id)

    with _in_flight_lock:
        if key in _steps_in_flight:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This document is already being processed.",
            )
        _steps_in_flight.add(key)

    try:
        yield
    finally:
        # Runs whatever happened — a finished step, a 502, a client that hung
        # up. A claim that outlived its request would wedge the document for the
        # lifetime of the process, which is worse than the bug being fixed.
        with _in_flight_lock:
            _steps_in_flight.discard(key)


# In plain English: keep a list of the documents currently being worked on. When
# a request arrives, look at the list — if this document is already on it, tell
# the caller "someone else is doing this one" and stop. Otherwise add it, do the
# work, and take it off again at the end no matter how the request ended. That
# is what stops two browser tabs paying twice to embed the very same pages and
# then tripping over each other's writes.


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

    # Counted rather than logged one line per box: `_items` re-parses the whole
    # document on every step, so per-box logging would repeat the same hundred
    # lines for every slice of a long ingest. One summary is enough to answer
    # the only question worth asking here — "the figures are missing, did we
    # throw them away?" — which previously had no answer anywhere at all,
    # because nothing ever passed `on_reject`.
    discarded: Counter[str] = Counter()
    regions = ingestion.find_images(
        data,
        mime_type,
        on_reject=lambda _page, _box, reason: discarded.update([reason]),
    )

    if discarded:
        logger.info("Image filter discarded %s", dict(discarded))

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
        for offset, region in enumerate(regions)
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


# A provider having a bad minute, not a bad file. Every one of these clears on
# its own, and the next step resumes from the chunks already written — so none
# of them should reach the generic handler at the bottom of `ingest_step`, which
# marks the document `failed` and tells the user to re-upload it. Re-uploading
# throws the resume point away and re-pays for every chunk already embedded, so
# the advice is not just unhelpful, it is expensive.
#
# `httpx.TransportError` is the base class for every connect, read, write and
# pool timeout, so one entry covers the whole family. Deliberately does not
# include `BadRequestError` or `UnprocessableEntityError`: those mean we sent
# something wrong, and retrying sends the identical thing again.
TRANSIENT_ERRORS = (
    TooManyRequestsError,
    InternalServerError,
    ServiceUnavailableError,
    GatewayTimeoutError,
    httpx.TransportError,
)

# In plain English: a list of the ways a service we depend on can be
# temporarily broken, as opposed to the ways the user's file can be broken.
# The difference matters because one of them is worth waiting out and the
# other is not.


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
    _claim: Annotated[None, Depends(claim_ingest_step)] = None,
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

    Only one step per document runs at a time — `claim_ingest_step` holds the
    ticket for the whole request and answers a second caller with a 409. Nothing
    below is safe to run twice at once: two steps would read the same resume
    point and both pay to embed the identical batch.
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

    # Before a cent is spent, and before the heavy CPU work: is there time to
    # finish anything? The loop below consults `budget` only *after* a batch has
    # been embedded and written, which meant a step arriving on a nearly-dead
    # token still paid in full — download, parse, every figure rendered, and one
    # Cohere call — and then lost the write to `"exp" claim timestamp check
    # failed`. Nothing was saved, so the browser came back and bought the same
    # batch again.
    #
    # A 401 rather than an empty step: `fetchWithToken` in
    # apps/web/src/lib/api.ts already answers one by fetching a fresh token and
    # replaying the request, so this costs a round trip and shows nothing on
    # screen. An empty `IngestStepOut` is not even available here — its
    # `chunks_total` is only knowable after the parse this guard exists to skip.
    #
    # The floor has to clear a download, a parse with every image rendered, one
    # embedding call and its insert. It must also stay well under a fresh
    # token's usable life — Clerk's session tokens live 60s, so anything near
    # `STEP_BUDGET_SECONDS` would refuse brand-new tokens too and wedge
    # ingestion permanently. That is the trap `chat.py` documents at
    # MIN_ANSWER_BUDGET_SECONDS, and it is the same trap here.
    if budget < MIN_STEP_BUDGET_SECONDS:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session token is about to expire. Please try again.",
            headers={"WWW-Authenticate": "Bearer"},
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
            except TRANSIENT_ERRORS as exc:
                # Not a failure — the embedding provider is saying "slower", or
                # is briefly down. End the step with whatever is already written
                # and let the browser come back. Treating this as fatal would
                # mark a perfectly good document `failed` for a condition that
                # clears itself in under a minute.
                #
                # Returning normally (rather than raising) is what makes this
                # recover with nobody watching: the step reports the same
                # `chunks_done` as last time, the browser reads that as no
                # progress, waits, and calls again.
                logger.warning(
                    "Embedding provider unavailable (%s) at chunk %s of %s",
                    type(exc).__name__,
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
            # anything was written at all. Also clamped to `total` (chunks
            # recomputed fresh this step): `written` can start from a resume
            # point saved in the database on a previous step, and if a parser
            # or config change makes this parse yield fewer chunks than before,
            # that saved number can point past the end of the new list.
            # In plain English: don't trust an old "how far we got" number more
            # than the list we actually have in hand right now — clamp it down
            # to fit before using it as an index.
            page=chunks[min(written, total) - 1].page_number if written else 0,
            pages=max((item.page_number for item in chunks), default=0),
            images_total=images_total,
        )

    except Exception as exc:
        # The download, the image uploads and the chunk insert all sit outside
        # the embedding loop's own guard above, so a provider blip on any of
        # them arrives here instead. The row must stay `processing`: the chunks
        # already written are still good, the resume point is still valid, and
        # Retry replays only what is left. Falling through to `failed` below is
        # what used to send somebody back to re-upload a perfectly fine file.
        #
        # `warning`, not `exception` — nothing here is a defect to go and read a
        # traceback about.
        if isinstance(exc, TRANSIENT_ERRORS):
            logger.warning(
                "Transient failure (%s) on document %s; leaving it resumable",
                type(exc).__name__,
                document_id,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "A service we depend on is briefly unavailable. Your "
                    "progress is saved — press Retry to pick up where this "
                    "left off."
                ),
            ) from exc

        # The message is stored on the row and shown to the user, so it has to
        # say something real — "Nothing could be read from this file…" above, for
        # instance. Chunks already written stay: a retry resumes from them.
        logger.exception("Ingestion step failed for document %s", document_id)

        # `ValueError` is this codebase talking: `ingestion.parse` on a type it
        # cannot read, `_parse_txt` on an encoding it cannot recover, and the
        # empty-file check above. Those sentences were written to be read by the
        # person who uploaded the file.
        #
        # Everything else is a library or a provider talking, and its text is
        # written for whoever is reading the log — pymupdf's "cannot find
        # startxref", a storage client quoting the request it just made. That
        # used to go straight to `str(exc)`, onto the screen, and into the
        # `documents.error` column that `document-list.tsx:207` renders
        # verbatim. What lands in a column the user reads is a decision, not
        # whatever the nearest dependency happened to phrase.
        if isinstance(exc, ValueError):
            message = str(exc) or "This file could not be read."
        else:
            message = (
                "Something went wrong while reading this file. "
                "Please try again, or re-upload it."
            )
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


@router.get(
    "/{document_id}/images/{chunk_id}",
    # Without this FastAPI would advertise `application/json` in the schema and
    # try to serialise the return value as JSON.
    response_class=Response,
    responses={200: {"content": {"image/jpeg": {}}}},
)
def get_chunk_image(
    _user_id: CurrentUser,
    supabase: SupabaseClient,
    document_id: UUID,
    chunk_id: UUID,
) -> Response:
    """The picture behind one image chunk, so a cited figure can be shown.

    Day 6b put page images in the index and Day 7 started sending them to the
    model, but the browser was only ever told an image chunk's Storage *path* —
    a string it has no credential to fetch. So the one thing the reader most
    wants to see when a figure is cited was the one thing they could not see.

    The chunk is addressed by id, never by path. A path parameter would be a
    string the caller chooses, and the whole `documents` bucket is one namespace
    keyed by Clerk id — so it would rest entirely on the storage policy, with
    this handler contributing nothing. An id is looked up through
    `chunks_isolation`, which returns nothing at all for a row that is not the
    caller's, and `document_id` is matched too so a mismatched pair is a 404
    rather than a quietly-served image from another document.
    """
    try:
        found = (
            supabase.table("chunks")
            .select("image_path, chunk_type")
            .eq("id", str(chunk_id))
            .eq("document_id", str(document_id))
            .limit(1)
            .execute()
        )
    except Exception as exc:
        logger.exception("Could not look up image chunk %s", chunk_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not load that image. Please try again.",
        ) from exc

    row = found.data[0] if found.data else None

    # One 404 for all three misses — not this user's chunk, not an image chunk,
    # or an image row whose path was never written. They are the same fact to
    # the reader ("there is no picture here"), and distinguishing them would
    # tell someone probing ids which ones exist.
    if not row or row.get("chunk_type") != "image" or not row.get("image_path"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="That image is not available.",
        )

    try:
        jpeg = ingestion.download(supabase, row["image_path"])
    except Exception as exc:
        # Same rule as `rag.load_images`: the exception's class and message, and
        # never the response body, which a storage client can fill with echoed
        # request headers.
        logger.warning(
            "Could not download image chunk %s (%s: %s)",
            chunk_id,
            type(exc).__name__,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="That image is not available.",
        ) from exc

    return Response(
        content=jpeg,
        media_type="image/jpeg",
        # A page image never changes: ingestion writes it once and delete is the
        # only thing that touches it again. `private` keeps it in this reader's
        # browser and out of any shared proxy, which matters because the URL is
        # only safe by virtue of the token that was sent with it.
        headers={"Cache-Control": "private, max-age=3600"},
    )

    # In plain English: look up the chunk the browser asked for, but only among
    # the chunks the database is willing to show this person. If it is not
    # theirs, is not a picture, or has no file behind it, reply "not available"
    # — the same reply in each case, so nobody can use the difference to work
    # out which ids are real. Otherwise fetch the JPEG out of storage and hand
    # the raw bytes back, telling the browser it may keep its own copy for an
    # hour but must not let any shared cache do the same.


@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    # The same ticket `ingest_step` takes, and the reason is finding 21: a step
    # uploads its JPEG *before* inserting the row that names it, so a delete
    # running alongside one could remove every object it knew about and then
    # have a fresh, unreferenced file land behind it — unreachable by any future
    # delete, and billed for as long as the bucket exists. Holding the same
    # claim means a delete and a step cannot overlap at all, so there is no
    # window for that upload to land in.
    #
    # A caller mid-ingest gets the claim's 409 rather than a delete. That is
    # honest — the document really is busy — and a step is bounded by
    # STEP_BUDGET_SECONDS, so the wait is seconds, not minutes.
    dependencies=[Depends(claim_ingest_step)],
)
async def delete_document(
    user_id: CurrentUser,
    supabase: SupabaseClient,
    document_id: UUID,
) -> None:
    """Delete a document's file, its pictures, and its row.

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
    #
    # Asked of Storage rather than of the `chunks` table, which is the second
    # half of finding 21. The old query found the images that made it into a
    # row; a step killed between uploading a JPEG and inserting that row leaves
    # a file no row has ever named, and no query could ever find it. The folder
    # is the truth about what exists, so the folder is what gets listed — and
    # this now cleans up orphans left by every such step before today.
    #
    # `img-N.jpg` all live under `{user}/{document}/`, while the uploaded file
    # itself sits one level up as `{user}/{document}.{ext}`, so it is added
    # separately. `foldername(name)[1]` is still the user id either way, which
    # is what the storage policy in 001_init.sql checks.
    image_prefix = f"{user_id}/{document_id}"

    try:
        stored = supabase.storage.from_(BUCKET).list(
            image_prefix,
            # Above MAX_IMAGES_PER_DOCUMENT with room for orphans. The default
            # is 100, which a 75-image document plus strays could reach — and a
            # truncated listing here fails the exact way this fix exists to
            # prevent, by leaving files behind and reporting success.
            {"limit": 1000},
        )
    except Exception as exc:
        logger.exception("Could not list image files for document %s", document_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not delete the document. Please try again.",
        ) from exc

    paths = [file_path] + [f"{image_prefix}/{obj['name']}" for obj in stored]

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


if __name__ == "__main__":
    # Imported here rather than at the top: these two are named only to prove
    # they are *excluded*, and nothing in the running app needs them.
    from cohere.errors import BadRequestError, UnprocessableEntityError

    # What must be transient: a provider having a bad minute. Getting this tuple
    # wrong is silent and expensive in both directions, which is why it is worth
    # a check at all. Drop a class from it and a Cohere blip goes back to marking
    # a good document `failed` and telling the reader to re-upload — throwing the
    # resume point away and re-paying for every chunk already embedded. Add too
    # much to it and a genuinely broken file stays `processing` forever, retrying
    # a parse that can never succeed.
    for transient in (
        TooManyRequestsError(body="slow down"),
        InternalServerError(body="upstream fell over"),
        ServiceUnavailableError(body="maintenance"),
        GatewayTimeoutError(body="took too long"),
        httpx.ReadTimeout("read timed out"),
        httpx.ConnectError("could not connect"),
        httpx.PoolTimeout("no free connection"),
    ):
        assert isinstance(transient, TRANSIENT_ERRORS), (
            f"{type(transient).__name__} must be treated as transient — as it is, "
            "a provider blip marks the user's file failed and tells them to re-upload"
        )

    # ...and what must not be. These mean the file or the request is wrong, and
    # retrying sends the identical thing again.
    for fatal in (
        ValueError("Nothing could be read from this file."),
        BadRequestError(body="malformed request"),
        UnprocessableEntityError(body="bad input"),
        RuntimeError("a real defect"),
    ):
        assert not isinstance(fatal, TRANSIENT_ERRORS), (
            f"{type(fatal).__name__} must not be retried forever — it will never succeed"
        )

    # The rate limit specifically: it was the only member before this change, and
    # it is the one that keeps a long ingest resumable rather than failed.
    assert issubclass(TooManyRequestsError, TRANSIENT_ERRORS), "rate limiting regressed to fatal"

    # The step budget floor. Both halves matter and they pull in opposite
    # directions, which is exactly why this is worth asserting rather than
    # eyeballing: too low and a step pays for work it cannot finish, too high
    # and it refuses tokens that were perfectly good and ingestion never runs
    # again.
    CLERK_SESSION_TOKEN_SECONDS = 60

    def budget_for(seconds_left: float) -> float:
        """The same arithmetic as `ingest_step`, with the clock passed in."""
        return min(
            STEP_BUDGET_SECONDS,
            seconds_left - TOKEN_SAFETY_MARGIN_SECONDS,
        )

    assert budget_for(CLERK_SESSION_TOKEN_SECONDS) >= MIN_STEP_BUDGET_SECONDS, (
        "a brand-new Clerk token would be refused — ingestion would 401 forever, "
        "which is the trap chat.py's MIN_ANSWER_BUDGET_SECONDS documents"
    )
    assert budget_for(8) < MIN_STEP_BUDGET_SECONDS, (
        "a nearly-dead token would still be allowed to embed a batch it cannot save"
    )
    assert MIN_STEP_BUDGET_SECONDS < STEP_BUDGET_SECONDS, (
        "the floor must sit below the ceiling or no step can ever run"
    )

    print(
        f"OK - {len(TRANSIENT_ERRORS)} transient error classes, fatal ones excluded; "
        f"step floor {MIN_STEP_BUDGET_SECONDS}s admits a fresh token, refuses a dying one"
    )
