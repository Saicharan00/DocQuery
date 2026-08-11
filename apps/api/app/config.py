from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# The single .env at the repo root, mirroring .env.example beside it — one file
# for both apps rather than a copy of the same secrets per app.
#
# Anchored to this file's own location, not to `.env` as a bare relative path.
# A relative path resolves against the working directory, so it only worked when
# uvicorn happened to be launched from the repo root and silently reported every
# field as "missing" from anywhere else.
#
# Found by searching upward rather than by counting levels. `parents[3]` was
# correct locally (config.py -> app -> api -> apps -> repo root) and crashed on
# Railway with `IndexError: 3`: the service's root is `apps/api`, so the file
# deploys to `/app/app/config.py` and there is no fourth parent above it. The
# app never finished importing, so it never answered a health check.
#
# `None` when nothing is found, which is the right answer on Railway — it injects
# real environment variables and there is no file to read. pydantic-settings
# accepts that and reads the environment alone.
ENV_FILE = next(
    (
        parent / ".env"
        for parent in Path(__file__).resolve().parents
        if (parent / ".env").is_file()
    ),
    None,
)

# In plain English: walk up the folders above this file one at a time, and stop at
# the first one that actually contains a `.env`. `next(..., None)` takes that first
# match, or hands back None if the walk finishes without finding one. Searching
# for the file cannot go wrong when the folder layout differs between machines,
# where counting levels breaks the moment anything moves.


class Settings(BaseSettings):
    """Every env var the API reads, in one place.

    Values come from the process environment (Railway) or the local .env file.
    Never log or echo these — see the secrets rules in CLAUDE.md.
    """

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        # Without this, a startup validation failure prints the settings it did
        # manage to load as "context" — putting fragments of real keys into the
        # traceback, and on Railway into stored deployment logs. The error still
        # names the field that is missing, which is the half worth having.
        hide_input_in_errors=True,
    )

    # Supabase
    supabase_url: str
    supabase_anon_key: str

    # Clerk. The issuer looks like https://<slug>.clerk.accounts.dev
    clerk_jwt_issuer: str

    # Cohere, for embeddings only. Chat goes through LiteLLM. No default, so a
    # missing key stops the app at startup with a clear message rather than
    # failing halfway through somebody's first upload.
    cohere_api_key: str

    # Chat models, via LiteLLM. Unlike cohere_api_key these are optional: with one
    # provider configured the app still boots and only requests naming the other
    # one fail, which is what makes it possible to build and test against Gemini
    # alone. The cost is that a typo in the env var name surfaces as a runtime
    # "not configured" error instead of a startup crash.
    #
    # These get passed to LiteLLM explicitly as api_key=, never left to its
    # implicit environment lookup: pydantic-settings reads .env into this object
    # but never exports to os.environ, so an implicit lookup would find the key on
    # Railway (real env vars) and miss it locally. A bug that only appears on one
    # machine is the worst kind.
    gemini_api_key: str | None = None
    openai_api_key: str | None = None

    # Spend guards. Every embedding is billed to my own Cohere key now that BYOK
    # is gone, so these protect a card, not a free quota.
    max_documents_per_day: int = 15
    global_daily_document_limit: int = 200

    # The same guard for chat. Counted per user message, since one user message is
    # one LLM call — the assistant row is its result, not a second charge.
    max_messages_per_day: int = 50
    global_daily_message_limit: int = 500

    # Comma-separated list of browser origins allowed to call this API.
    # Day 4 adds the Vercel URL here — no code change needed, just the env var.
    cors_origins: str = "http://localhost:3000"

    @property
    def jwks_url(self) -> str:
        """Where Clerk publishes the public keys we verify JWT signatures against."""
        return f"{self.clerk_jwt_issuer.rstrip('/')}/.well-known/jwks.json"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
