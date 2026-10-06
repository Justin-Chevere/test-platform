from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Env vars (or a .env file) override these defaults, so the same code runs
    # against SQLite locally and Postgres in a deployed environment.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "test-platform"
    database_url: str = "sqlite:///./testplatform.db"

    # Host names the API answers to. See create_app() for why this matters.
    allowed_hosts: list[str] = ["localhost", "127.0.0.1"]

    # Where workers check out code and build virtualenvs. Unset: the system temp folder.
    workspace_dir: Path | None = None
    # How often an idle worker checks for queued runs.
    worker_poll_seconds: float = Field(default=1.0, gt=0)
    # Each run keeps the end of its log, where failures show up, up to this size.
    max_log_bytes: int = Field(default=200_000, gt=0)


@lru_cache
def get_settings() -> Settings:
    return Settings()
