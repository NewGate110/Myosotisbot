"""Validated configuration shared by the bot and isolated media worker."""

import os
from dataclasses import dataclass, fields


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class Limits:
    upload_bytes: int = 45_000_000
    download_bytes: int = 200_000_000
    temp_bytes: int = 500_000_000
    source_seconds: int = 600
    gif_seconds: int = 30
    job_seconds: int = 600
    process_seconds: int = 120
    active_jobs: int = 1

    @classmethod
    def from_env(cls):
        names = {
            "upload_bytes": "MAX_UPLOAD_BYTES",
            "download_bytes": "MAX_DOWNLOAD_BYTES",
            "temp_bytes": "MAX_TEMP_BYTES",
            "source_seconds": "MAX_SOURCE_SECONDS",
            "gif_seconds": "MAX_GIF_SECONDS",
            "job_seconds": "MAX_JOB_SECONDS",
            "process_seconds": "MAX_PROCESS_SECONDS",
            "active_jobs": "MAX_ACTIVE_JOBS",
        }
        values = {}
        for field in fields(cls):
            name = names[field.name]
            try:
                values[field.name] = int(os.environ.get(name, field.default))
            except ValueError:
                raise ConfigurationError(f"{name} must be a positive integer.") from None
            if values[field.name] <= 0:
                raise ConfigurationError(f"{name} must be a positive integer.")
        if values["upload_bytes"] > 49_000_000:
            raise ConfigurationError("MAX_UPLOAD_BYTES must be at most 49000000 for the standard Telegram API.")
        return cls(**values)


@dataclass(frozen=True)
class Settings:
    token: str
    owner_id: int
    group_id: int
    limits: Limits

    @classmethod
    def from_env(cls):
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ConfigurationError("Set TELEGRAM_BOT_TOKEN in .env or the environment.")
        try:
            owner = int(os.environ.get("TELEGRAM_OWNER_USER_ID", ""))
            group = int(os.environ.get("TELEGRAM_ALLOWED_GROUP_ID", ""))
        except ValueError:
            raise ConfigurationError("Set numeric TELEGRAM_OWNER_USER_ID and TELEGRAM_ALLOWED_GROUP_ID.") from None
        if owner <= 0 or group >= 0:
            raise ConfigurationError("The owner ID must be positive and the group ID must be negative.")
        return cls(token, owner, group, Limits.from_env())
