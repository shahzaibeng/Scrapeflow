"""Application configuration loaded from environment variables / `.env`."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DEFAULT_USER_AGENTS: list[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0",
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Database ---------------------------------------------------------
    database_url: str = "sqlite+aiosqlite:///./ppra_tenders.db"
    db_echo: bool = False
    db_upsert_batch_size: int = Field(default=200, ge=1, le=1000)

    # --- Source portal ----------------------------------------------------
    epms_base_url: str = "https://epms.ppra.gov.pk"
    epms_listing_path: str = "/public/tenders/active-tenders"
    json_api_url: str | None = None

    # --- Scraper behaviour ------------------------------------------------
    request_timeout: float = Field(default=30.0, gt=0)
    connect_timeout: float = Field(default=15.0, gt=0)
    max_retries: int = Field(default=4, ge=0)
    retry_backoff_base: float = Field(default=1.5, gt=0)
    retry_backoff_max: float = Field(default=60.0, gt=0)
    min_delay: float = Field(default=1.0, ge=0)
    max_delay: float = Field(default=2.5, ge=0)
    fetch_details: bool = True
    detail_concurrency: int = Field(default=4, ge=1, le=32)
    detail_delay: float = Field(default=0.4, ge=0)
    max_pages: int = Field(default=0, ge=0)
    verify_ssl: bool = True
    user_agents: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_USER_AGENTS)
    )

    # --- Misc ---------------------------------------------------------------
    source_timezone: str = "Asia/Karachi"
    log_level: str = "INFO"
    log_dir: str = "logs"

    @field_validator("user_agents", mode="before")
    @classmethod
    def _split_user_agents(cls, value: object) -> object:
        if isinstance(value, str):
            agents = [ua.strip() for ua in value.split(",") if ua.strip()]
            return agents or list(DEFAULT_USER_AGENTS)
        return value

    @field_validator("json_api_url", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("database_url")
    @classmethod
    def _normalise_db_url(cls, value: str) -> str:
        """Force async drivers so the same URL works with the async engine."""
        url = value.strip()
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        if url.startswith("postgresql+psycopg2://"):
            url = "postgresql+asyncpg://" + url[len("postgresql+psycopg2://"):]
        elif url.startswith("postgresql://"):
            url = "postgresql+asyncpg://" + url[len("postgresql://"):]
        elif url.startswith("sqlite://") and not url.startswith("sqlite+aiosqlite://"):
            url = "sqlite+aiosqlite://" + url[len("sqlite://"):]
        return url

    @field_validator("source_timezone")
    @classmethod
    def _validate_tz(cls, value: str) -> str:
        ZoneInfo(value)  # raises if unknown
        return value

    @model_validator(mode="after")
    def _check_delays(self) -> "Settings":
        if self.max_delay < self.min_delay:
            self.max_delay = self.min_delay
        return self

    @property
    def listing_url(self) -> str:
        return self.epms_base_url.rstrip("/") + self.epms_listing_path

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.source_timezone)

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings: Settings = get_settings()
