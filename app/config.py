from functools import lru_cache
from pathlib import Path
from typing import Self

from pydantic import Field, model_validator
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
    # On Windows, keep it short: in a deep folder, the files a virtualenv installs can
    # exceed Windows' 260-character path limit, and creating it fails.
    workspace_dir: Path | None = None
    # How often an idle worker checks for queued runs.
    worker_poll_seconds: float = Field(default=1.0, gt=0)
    # Each run keeps the end of its log, where failures show up, up to this size.
    max_log_bytes: int = Field(default=200_000, gt=0)

    # While a worker executes a run, it sends a heartbeat this often. A running run with
    # no heartbeat for lease_seconds is presumed abandoned (its worker crashed or was
    # killed) and goes back to the queue, until it has been claimed max_attempts times.
    heartbeat_seconds: float = Field(default=10.0, gt=0)
    lease_seconds: float = Field(default=60.0, gt=0)
    max_attempts: int = Field(default=3, ge=1)

    @model_validator(mode="after")
    def _lease_outlasts_several_heartbeats(self) -> Self:
        # So one late or failed heartbeat never makes a live worker look dead.
        if self.lease_seconds < 3 * self.heartbeat_seconds:
            raise ValueError("lease_seconds must be at least 3 times heartbeat_seconds")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
