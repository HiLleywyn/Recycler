from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables / .env file."""

    # Database
    DATABASE_URL: str = "postgresql://discoin:discoin@localhost:5432/discoin"

    # Redis
    REDIS_URL: str = "redis://localhost:6379"

    # JWT  -  defaults match v1 (Config.JWT_SECRET / JWT_EXPIRE_SECONDS)
    JWT_SECRET: str = "change-me-in-production"
    JWT_EXPIRE_SECONDS: int = 604800  # 7 days (matches v1 default)
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    # Discord OAuth2
    DISCORD_CLIENT_ID: str = ""
    DISCORD_CLIENT_SECRET: str = ""
    DISCORD_REDIRECT_URI: str = ""

    # Server
    API_PORT: int = 8080

    # CORS  -  set CORS_ORIGINS env var as comma-separated list for production
    # e.g. CORS_ORIGINS='["https://discoin.example.com","http://localhost:3000"]'
    CORS_ORIGINS: list[str] = ["http://localhost:3000", "http://localhost:8080"]

    # Rate limiting (requests per 10-second window)
    RATE_LIMIT_PUBLIC: int = 60
    RATE_LIMIT_AUTH: int = 120
    RATE_LIMIT_ADMIN: int = 240

    # Proxy headers  -  set to True only when running behind a trusted reverse proxy
    # (nginx, Cloudflare, etc.) to extract the real client IP from X-Forwarded-For.
    # Leave False (default) to avoid IP spoofing via forged headers.
    TRUST_PROXY_HEADERS: bool = False

    # Security system  -  set SECURITY_SYSTEM=false to disable the security engine entirely
    SECURITY_SYSTEM: bool = True

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": True,
        "extra": "ignore",
    }


@lru_cache()
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
