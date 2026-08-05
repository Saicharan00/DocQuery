from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Every env var the API reads, in one place.

    Values come from the process environment (Railway) or a local .env file.
    Never log or echo these — see the secrets rules in CLAUDE.md.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
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

    # Spend guards. Every embedding is billed to my own Cohere key now that BYOK
    # is gone, so these protect a card, not a free quota.
    max_documents_per_day: int = 15
    global_daily_document_limit: int = 200

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
