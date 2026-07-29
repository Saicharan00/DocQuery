from fastapi import APIRouter

from app.deps import CurrentUser

router = APIRouter(tags=["me"])


@router.get("/me")
async def me(user_id: CurrentUser) -> dict[str, str]:
    """Echo back who the verified JWT says you are. Requires auth."""
    return {"user_id": user_id}
