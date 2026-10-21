"""Configuration from the environment (or a ``.env`` file) — never from code."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    index_path: Path = Path("build/index.bin")  # env: INDEX_PATH
    app_name: str = "findex"
    default_k: int = 10
    max_k: int = 100
    # "sync" registers /search as `def` (runs in the threadpool); "async" as `async def`
    # (runs on the event loop). Only for the Lab 7 measurement — sync is the right choice.
    search_mode: Literal["sync", "async"] = "sync"  # env: SEARCH_MODE
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
