import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.deps import jwks_cache
from app.routers import chat, conversations, documents, feedback, health, me

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


class CloseAbandonedStreams:
    """Make sure a streaming response is closed even when nobody is listening.

    Written as raw ASGI rather than with `@app.middleware("http")`, which builds
    a `BaseHTTPMiddleware` — that one consumes the response through a queue and
    is well known for interfering with streaming, which is the one thing this
    file must not break.

    The problem it solves, measured on 2026-08-14 rather than assumed: when a
    browser goes away mid-answer, Starlette abandons the response generator
    instead of closing it. `iterate_in_threadpool` (concurrency.py:51-59) has no
    `finally`, so `/chat`'s generator was left suspended at a `yield` for as
    long as it took the garbage collector to notice — and with it went the
    answer (never saved), the trace (stuck "running"), and the model's stream
    (open, and billed for).

    A middleware fixes it because a `finally` around `await self.app(...)` runs
    on both of Starlette's exit paths: the ASGI 2.4+ one that raises
    `ClientDisconnect` outward, and the older task-group one that just returns.
    A `BackgroundTask` looked simpler and is only correct on the second — on the
    first, `StreamingResponse.__call__` raises before it ever reaches
    `self.background()`.

    A handler opts in by appending a zero-argument callable to
    `scope["state"]["stream_cleanup"]`.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            # Websockets and the lifespan messages have no streaming response to
            # clean up, and must pass through untouched.
            await self.app(scope, receive, send)
            return

        cleanups: list = []
        scope.setdefault("state", {})["stream_cleanup"] = cleanups

        try:
            await self.app(scope, receive, send)
        finally:
            for close in cleanups:
                try:
                    close()
                except Exception:
                    # Never let cleanup replace the real outcome of the request.
                    # `close()` on a generator that is somehow still running
                    # raises ValueError, and a raise here would mask whatever
                    # actually happened above.
                    logger.exception("Could not close an abandoned response stream")

    # In plain English: before the request runs, put an empty list of "things to
    # tidy up" where the handler can reach it. Run the request. Then, no matter
    # how it ended — finished, failed, or the reader closed the tab — go through
    # that list and tidy each one up. That is what turns "the browser vanished"
    # from a stream nobody ever switched off into an ordinary ending.


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

# Added last, so it sits outermost and its `finally` is the last thing to run —
# outside CORS, and outside anything that could swallow a disconnect on the way
# out.
app.add_middleware(CloseAbandonedStreams)

app.include_router(health.router)
app.include_router(me.router)
app.include_router(documents.router)
app.include_router(chat.router)
app.include_router(conversations.router)
app.include_router(feedback.router)


# ---------------------------------------------------------------------------
# Self-check
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # The middleware only, driven directly. Needs no server, no token and no
    # network:
    #   apps\api> .venv\Scripts\python.exe -m app.main
    import asyncio

    async def _check() -> None:
        closed: list[str] = []

        async def ok_app(scope, receive, send):
            scope["state"]["stream_cleanup"].append(lambda: closed.append("ok"))

        async def dying_app(scope, receive, send):
            # Stands in for the real failure this exists for: Starlette raises
            # ClientDisconnect outward on the ASGI 2.4+ path, so cleanup has to
            # survive an exception on the way out — not merely a tidy return.
            scope["state"]["stream_cleanup"].append(lambda: closed.append("disconnected"))
            raise RuntimeError("client went away")

        async def noisy_app(scope, receive, send):
            scope["state"]["stream_cleanup"].append(
                lambda: (_ for _ in ()).throw(ValueError("close failed"))
            )

        http = {"type": "http"}

        await CloseAbandonedStreams(ok_app)(dict(http), None, None)
        assert closed == ["ok"], closed

        try:
            await CloseAbandonedStreams(dying_app)(dict(http), None, None)
        except RuntimeError:
            pass
        else:
            raise AssertionError("the request's own error was swallowed by cleanup")
        assert closed == ["ok", "disconnected"], (
            f"cleanup did not run when the request died: {closed}"
        )

        # A cleanup that itself fails must not replace the request's outcome or
        # stop the ones after it.
        await CloseAbandonedStreams(noisy_app)(dict(http), None, None)

        # Lifespan and websocket scopes have nothing to clean and must pass
        # straight through, untouched.
        passed: list[str] = []

        async def lifespan_app(scope, receive, send):
            passed.append(scope["type"])
            assert "state" not in scope, "a non-http scope was given stream state"

        await CloseAbandonedStreams(lifespan_app)({"type": "lifespan"}, None, None)
        assert passed == ["lifespan"], passed

    asyncio.run(_check())

    print(
        f"OK - {len(app.routes)} routes registered; abandoned-stream cleanup runs on a "
        "clean exit, on a failed one, and survives a cleanup that itself throws"
    )
