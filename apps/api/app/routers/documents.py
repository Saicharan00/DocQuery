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
from pathlib import PurePosixPath
from uuid import UUID, uuid4

from fastapi import APIRouter, File, HTTPException, UploadFile, status

from app.deps import CurrentUser, SupabaseClient
from app.models.document import DocumentOut

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
