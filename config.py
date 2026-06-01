"""Configuration loading for the discomp3play bot."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Config:
    token: str | None
    music_dir: str
    guild_ids: list[int]
    idle_timeout: int


def load_config() -> Config:
    """Load configuration from environment variables.

    Token validation is deferred to startup (see bot.main) so the module can
    be imported for testing without a real token.
    """
    token = os.getenv("DISCORD_TOKEN")
    music_dir = os.getenv("MUSIC_DIR", "music")
    raw_guilds = os.getenv("GUILD_ID", "")
    guild_ids = [int(s) for s in (p.strip() for p in raw_guilds.split(",")) if s]
    idle_timeout = os.getenv("IDLE_TIMEOUT", "300")
    return Config(
        token=token,
        music_dir=music_dir,
        guild_ids=guild_ids,
        idle_timeout=int(idle_timeout),
    )
