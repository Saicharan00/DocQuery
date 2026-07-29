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
    )

    # Supabase
    supabase_url: str
    supabase_anon_key: str

    # Clerk. The issuer looks like https://<slug>.clerk.accounts.dev
    clerk_jwt_issuer: str

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
