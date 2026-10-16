"""Application settings, loaded from environment variables only."""

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def normalize_db_url(v: str) -> str:
    """Railway gives postgres:// or postgresql://; SQLAlchemy async needs postgresql+asyncpg://."""
    if v.startswith("postgres://"):
        v = "postgresql://" + v[len("postgres://"):]
    if v.startswith("postgresql://"):
        v = "postgresql+asyncpg://" + v[len("postgresql://"):]
    return v


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Telegram (bot-panel; api_id/api_hash are also required for a Telethon bot)
    bot_token: str
    tg_api_id: int
    tg_api_hash: str
    admin_tg_id: int = Field(description="Your personal Telegram user id; only this user can use the bot")
    seller_username: str = Field(description="@username of the selling account, used as contact in listings")

    # Storage
    database_url: str
    data_dir: Path = Path("/data")

    # Claude
    anthropic_api_key: str
    ai_model: str = "claude-sonnet-5-5"
    ai_model_fast: str = "claude-haiku-5-5"
    ai_web_search_max_uses: int = 5
    ai_max_photos_per_item: int = 8

    # Apify (Bazaraki comparables)
    apify_token: str | None = None
    apify_bazaraki_actor: str = "logiover~bazaraki-scraper"
    apify_max_listings: int = 30
    apify_timeout_s: int = 240

    # Pipeline
    pipeline_concurrency: int = 3
    photo_max_side: int = 2560
    photo_ai_max_side: int = 1568
    photo_thumb_side: int = 480

    # HTTP
    port: int = 8080
    log_level: str = "INFO"

    @field_validator("database_url")
    @classmethod
    def _async_driver(cls, v: str) -> str:
        return normalize_db_url(v)

    @field_validator("seller_username")
    @classmethod
    def _strip_at(cls, v: str) -> str:
        return v.lstrip("@").strip()

    def storage_problem(self) -> str | None:
        """On Railway, files outside the mounted volume vanish on every redeploy."""
        mount = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
        if os.environ.get("RAILWAY_ENVIRONMENT") and not mount:
            return "к сервису не подключён Volume — фото пропадут при передеплое. Подключи Volume с mount path /data."
        if mount and not self.data_dir.resolve().is_relative_to(Path(mount).resolve()):
            return (f"DATA_DIR={self.data_dir} не на Volume ({mount}) — фото пропадут при передеплое. "
                    f"Удали переменную DATA_DIR или поставь DATA_DIR={mount}.")
        return None

    @property
    def photos_dir(self) -> Path:
        return self.data_dir / "photos"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
