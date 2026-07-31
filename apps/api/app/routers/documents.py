"""Document CRUD.

Note what is deliberately absent from every query here: a `WHERE user_id = ...`
filter. Row-level security in `001_init.sql` does that, using the `sub` claim of
the Clerk token the Supabase client forwards. Filtering here as well would mask
whether RLS is actually doing its job — see the RLS rule in CLAUDE.md.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status

from app.deps import CurrentUser, SupabaseClient
from app.models.document import DocumentOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/documents", tags=["documents"])


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
