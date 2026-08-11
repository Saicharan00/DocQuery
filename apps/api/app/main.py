import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.deps import jwks_cache
from app.routers import chat, documents, health, me

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Warm the Clerk key cache so the first authenticated request isn't slow."""
    settings = get_settings()
    try:
        await jwks_cache.refresh(settings.jwks_url)
    except Exception as exc:
        # Deliberately not fatal: /health must stay up so Railway sees a live
        # service and we can read the logs. Authenticated routes will retry the
        # fetch and return 503 with a clear message until this resolves.
        logger.warning(
            "Startup fetch of Clerk signing keys failed (%s). "
            "Auth routes will retry per-request; check CLERK_JWT_ISSUER.",
            exc,
        )
    yield


settings = get_settings()

app = FastAPI(
    title="DocQuery API",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(me.router)
app.include_router(documents.router)
app.include_router(chat.router)
