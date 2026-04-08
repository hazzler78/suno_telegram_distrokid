from __future__ import annotations

import os
from pathlib import Path
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Paths(BaseModel):
	work_dir: Path = Field(default=Path(os.getenv("WORK_DIR", "work")))
	download_dir: Path = Field(default=Path(os.getenv("DOWNLOAD_DIR", "work/downloads")))
	cover_dir: Path = Field(default=Path(os.getenv("COVER_DIR", "work/covers")))
	cookies_dir: Path = Field(default=Path(os.getenv("COOKIES_DIR", "work/cookies")))
	output_dir: Path = Field(default=Path(os.getenv("OUTPUT_DIR", "work/output")))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str = Field(alias="TELEGRAM_BOT_TOKEN")

    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    stable_diffusion_api_key: str | None = Field(default=None, alias="STABLE_DIFFUSION_API_KEY")

    distrokid_email: str | None = Field(default=None, alias="DISTROKID_EMAIL")
    distrokid_password: str | None = Field(default=None, alias="DISTROKID_PASSWORD")

    # Suno web login for download flow
    suno_email: str | None = Field(default=None, alias="SUNO_EMAIL")
    suno_password: str | None = Field(default=None, alias="SUNO_PASSWORD")

    # Discord OAuth for Suno login
    discord_email: str | None = Field(default=None, alias="DISCORD_EMAIL")
    discord_password: str | None = Field(default=None, alias="DISCORD_PASSWORD")

    log_level: str = Field(default=os.getenv("LOG_LEVEL", "INFO"))
    debug: bool = Field(default=os.getenv("DEBUG", "0") == "1")

    paths: Paths = Paths()

    def ensure_directories(self) -> None:
        self.paths.work_dir.mkdir(parents=True, exist_ok=True)
        self.paths.download_dir.mkdir(parents=True, exist_ok=True)
        self.paths.cover_dir.mkdir(parents=True, exist_ok=True)
        self.paths.cookies_dir.mkdir(parents=True, exist_ok=True)
        self.paths.output_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_directories()
