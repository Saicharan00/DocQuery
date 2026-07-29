from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe. No auth — Railway's healthcheck hits this."""
    return {"status": "ok"}
