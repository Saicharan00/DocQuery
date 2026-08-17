"""Request-scoped dependencies: who is calling, and how we talk to the database.

Two things live here.

1. `get_current_user` — verifies the Clerk JWT on the request and returns the
   Clerk user id (the token's `sub` claim). This is a fast fail-closed check.

2. `get_supabase_client` — builds a Supabase client that carries the caller's
   JWT, so PostgREST populates `request.jwt.claims` and the RLS policies from
   `001_init.sql` decide what rows come back.

RLS is the security boundary, not (1). (1) exists so unauthenticated requests
get a clean 401 instead of an opaque database error.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Annotated, Any

import httpx
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import jwt
from jose.exceptions import ExpiredSignatureError, JWTError
from supabase import Client, ClientOptions, create_client

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Clerk rotates signing keys rarely. Re-fetch hourly, or immediately when a
# token arrives signed by a `kid` we haven't seen.
JWKS_TTL_SECONDS = 3600
JWKS_MIN_REFETCH_SECONDS = 30

# After a refresh fails, keep serving the cached key set for this long before
# trying Clerk again. The refresh runs under the lock with a 10s timeout, so
# without this every queued request pays its own 10s wait and the pile-up
# outlives the outage that caused it.
JWKS_FAILURE_BACKOFF_SECONDS = 30

# auto_error=False so a missing header reaches our code and we control the
# response shape, rather than FastAPI raising a 403 for a missing bearer token.
bearer_scheme = HTTPBearer(auto_error=False)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


class JwksCache:
    """In-memory cache of Clerk's public signing keys."""

    def __init__(self) -> None:
        self._keys: dict[str, dict[str, Any]] = {}
        self._fetched_at: float = 0.0
        # When the last refresh *failed*. Kept separate from `_fetched_at` on
        # purpose: marking a failure as a fetch would make the stale keys look
        # fresh and stop us retrying for a full hour.
        self._failed_at: float = float("-inf")
        self._lock = asyncio.Lock()

    @property
    def is_populated(self) -> bool:
        return bool(self._keys)

    async def refresh(self, jwks_url: str) -> None:
        """Fetch the key set. Raises on network or malformed-response failure."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(jwks_url)
            response.raise_for_status()
            payload = response.json()

        keys = {key["kid"]: key for key in payload.get("keys", []) if "kid" in key}
        if not keys:
            raise ValueError(f"No usable signing keys returned by {jwks_url}")

        self._keys = keys
        self._fetched_at = time.monotonic()
        logger.info("Loaded %d Clerk signing key(s)", len(keys))

    async def get_key(self, kid: str, jwks_url: str) -> dict[str, Any]:
        """Return the JWK for `kid`, refetching if it's unknown or stale."""
        age = time.monotonic() - self._fetched_at
        needs_fetch = not self._keys or age > JWKS_TTL_SECONDS

        if not needs_fetch and kid in self._keys:
            return self._keys[kid]

        async with self._lock:
            # Another request may have refreshed while we waited for the lock.
            if kid in self._keys and time.monotonic() - self._fetched_at <= JWKS_TTL_SECONDS:
                return self._keys[kid]

            if self._keys and time.monotonic() - self._fetched_at < JWKS_MIN_REFETCH_SECONDS:
                # Don't hammer Clerk when a bad `kid` is retried in a loop.
                #
                # 503, not 401, and the difference is the whole of finding 5.
                # Inside this window we are *declining to look*, so we genuinely
                # cannot tell a junk token from a real key rotation — and a real
                # rotation is exactly when every token in existence carries a
                # `kid` we have not seen. Answering 401 told the browser "your
                # token is stale", so `fetchWithToken` retried immediately with
                # `skipCache`, hit the same unexpired window, got a second 401
                # and reported `SessionExpiredError`. A healthy user was told to
                # sign in again, and signing in again did not help, because the
                # new token carried the same new key.
                #
                # 503 says what is true — "ask me again shortly" — and the
                # browser does not burn the window trying to fix a token that
                # was never the problem.
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Still verifying your session. Please try again in a moment.",
                )

            if time.monotonic() - self._failed_at < JWKS_FAILURE_BACKOFF_SECONDS:
                # A refresh failed moments ago, so don't attempt another one
                # yet — for any `kid`. The fetch runs *under this lock* with a
                # 10s timeout, so retrying per request would serialise every
                # caller behind a dead endpoint and turn a blip at Clerk into a
                # longer outage of our own making. One attempt per backoff
                # window is enough to notice when Clerk comes back.
                if kid in self._keys:
                    return self._keys[kid]
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Cannot reach the authentication provider. Try again shortly.",
                )

            try:
                await self.refresh(jwks_url)
            except Exception as exc:
                self._failed_at = time.monotonic()
                logger.error("Could not fetch Clerk signing keys: %s", exc)
                if kid in self._keys:
                    # The point of the whole block. RS256 signing keys live for
                    # months and Clerk rotates them rarely, so a key already in
                    # hand is almost certainly still valid. Refusing it would
                    # 503 every authenticated request for the duration of a
                    # Clerk blip while this process holds the key required to
                    # serve them. The token's own `exp` and signature are still
                    # checked by the caller, and the staleness is bounded by
                    # JWKS_FAILURE_BACKOFF_SECONDS.
                    logger.warning(
                        "Serving cached Clerk signing key after a failed refresh"
                    )
                    return self._keys[kid]
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Cannot reach the authentication provider. Try again shortly.",
                ) from exc

        if kid not in self._keys:
            raise _unauthorized("Token signing key is not recognised.")
        return self._keys[kid]

    # In plain English, the whole of `get_key`: the token says which key signed
    # it. If we already have that key and our copy is less than an hour old,
    # hand it straight back — that is the overwhelmingly common path and it
    # touches the network not at all.
    #
    # Otherwise queue up, one request at a time, and look again — somebody
    # ahead in the queue may have just fetched what we need. If we already
    # fetched in the last thirty seconds and the key still is not there, don't
    # fetch again; that is usually somebody retrying a junk token in a loop. But
    # say "try again shortly" rather than "your token is bad", because from in
    # here those two look identical, and one of them is a real key rotation
    # where the token is perfectly fine.
    #
    # If a fetch failed in the last thirty seconds and we happen to hold the key
    # being asked for, use our old copy instead of trying again. And if a fetch
    # is worth attempting but fails, fall back to the old copy anyway when we
    # have it. These signing keys change perhaps twice a year, so an old copy is
    # nearly always the same copy — whereas refusing it would sign everybody out
    # of the app for as long as the login provider is having a bad minute, while
    # this server sits on the very key it needs.
    #
    # Only if the key is genuinely one we have never seen do we give up: either
    # "we cannot reach the login provider" if the fetch broke, or "we do not
    # recognise this token" if it worked and the key still was not there.


jwks_cache = JwksCache()


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> str:
    """Verify the bearer token and return the Clerk user id."""
    if credentials is None or not credentials.credentials:
        raise _unauthorized("Missing bearer token.")

    token = credentials.credentials

    try:
        header = jwt.get_unverified_header(token)
    except JWTError as exc:
        raise _unauthorized("Malformed token.") from exc

    kid = header.get("kid")
    if not kid:
        raise _unauthorized("Token header is missing a key id.")

    key = await jwks_cache.get_key(kid, settings.jwks_url)

    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            issuer=settings.clerk_jwt_issuer,
            # Clerk session tokens carry `azp`, not `aud`.
            options={"verify_aud": False},
        )
    except ExpiredSignatureError as exc:
        raise _unauthorized("Token has expired.") from exc
    except JWTError as exc:
        # Deliberately vague: never echo token contents back to the caller.
        raise _unauthorized("Token failed verification.") from exc

    user_id = claims.get("sub")
    if not user_id:
        raise _unauthorized("Token is missing the sub claim.")

    return str(user_id)


CurrentUser = Annotated[str, Depends(get_current_user)]


def get_token_expiry(
    _user_id: CurrentUser,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> float:
    """When the caller's token stops being valid, as a UNIX timestamp.

    Long-running work needs this. Ingestion embeds for tens of seconds and every
    write it makes rides on this token, so it has to know when its own credential
    dies — otherwise it works to a guessed budget and Supabase rejects a write
    partway through with `"exp" claim timestamp check failed`.

    A guessed budget is not good enough because Clerk hands the browser a
    *cached* token, refreshing only when it is nearly dead. A step can therefore
    begin with 15 seconds of validity left, not the full 60.

    Reading the claim without re-verifying is safe here: `get_current_user` is a
    dependency of this function, so FastAPI has already checked this exact
    token's signature, issuer and expiry before this line runs. Decoding again
    would only repeat that work.
    """
    if credentials is None:  # pragma: no cover — get_current_user already 401s
        raise _unauthorized("Missing bearer token.")

    expires_at = jwt.get_unverified_claims(credentials.credentials).get("exp")
    if expires_at is None:
        # Fail closed. A token with no expiry cannot be reasoned about, and
        # silently substituting a default would hand out a budget built on a
        # number nobody set.
        raise _unauthorized("Token is missing the exp claim.")

    return float(expires_at)


TokenExpiry = Annotated[float, Depends(get_token_expiry)]


def get_supabase_client(
    _user_id: CurrentUser,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Client:
    """A per-request Supabase client that acts as the calling user.

    The caller's JWT goes out on every PostgREST request, so Postgres sees
    `request.jwt.claims` and applies the RLS policies. We use the anon key here
    on purpose: the service role key would bypass RLS entirely.

    Depends on `get_current_user` so the token is always verified before we
    build a client with it.
    """
    if credentials is None:  # pragma: no cover — get_current_user already 401s
        raise _unauthorized("Missing bearer token.")

    return create_client(
        settings.supabase_url,
        settings.supabase_anon_key,
        options=ClientOptions(
            headers={"Authorization": f"Bearer {credentials.credentials}"},
            # We manage tokens ourselves; don't let a per-request client start
            # background refresh timers or try to persist a session.
            auto_refresh_token=False,
            persist_session=False,
        ),
    )


SupabaseClient = Annotated[Client, Depends(get_supabase_client)]
