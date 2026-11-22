"""Configuration settings for ShadowHeal backend application."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables and .env file.

    Unrelated variables (such as PostgreSQL credentials) are ignored to allow
    shared environment configurations.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    REDIS_HOST: str = Field(
        default="localhost",
        min_length=1,
        description="Hostname or IP address for the Redis server.",
    )
    REDIS_PORT: int = Field(
        default=6379,
        ge=1,
        le=65535,
        description="Port number for the Redis server (1-65535).",
    )
    REDIS_QUEUE_NAME: str = Field(
        default="shadowheal:events:raw",
        min_length=1,
        description="Non-empty Redis queue key name for raw incoming error events.",
    )


@lru_cache
def get_settings() -> Settings:
    """Return cached singleton instance of application settings."""
    return Settings()
