"""Runtime configuration for the Gemini web service, loaded from environment."""

import os
from pathlib import Path

# Load a local .env for non-Docker runs. Existing env vars win (override=False),
# so Docker Compose's injected values are never clobbered. No-op if unavailable.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


class Config:
    """Service configuration sourced from environment variables.

    Gemini accounts are NO LONGER configured here — they are managed at runtime
    through the admin UI and persisted to ``accounts_path``. The env cookies are
    only read once to seed the first account when the store is empty.
    """

    def __init__(self) -> None:
        self.api_key = os.getenv("SERVICE_API_KEY", "")
        data_dir = Path(os.getenv("DATA_DIR", "./data")).resolve()
        self.accounts_path = Path(
            os.getenv("ACCOUNTS_PATH", str(data_dir / "accounts.json"))
        ).resolve()
        self.proxy = os.getenv("GEMINI_PROXY") or None
        self.request_timeout = float(os.getenv("REQUEST_TIMEOUT", "300"))
        # Transient buffer for streamed media (mode="stream").
        self.media_cache_dir = Path(
            os.getenv("MEDIA_CACHE_DIR", str(data_dir / "cache"))
        ).resolve()
        self.media_cache_ttl = float(os.getenv("MEDIA_CACHE_TTL", "3600"))
        # Optional one-time seed for the first account (migration from .env).
        self.seed_1psid = os.getenv("GEMINI_SECURE_1PSID", "")
        self.seed_1psidts = os.getenv("GEMINI_SECURE_1PSIDTS", "")

    def validate(self) -> None:
        """Fail fast on missing mandatory configuration."""

        if not self.api_key:
            raise RuntimeError("Missing required env var: SERVICE_API_KEY")


config = Config()
