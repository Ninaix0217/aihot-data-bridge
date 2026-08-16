from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    api_base_url: str = "https://aihot.virxact.com"
    connect_timeout_seconds: float = 5.0
    request_timeout_seconds: float = 20.0
    max_retries: int = 2
    max_pages: int = 10

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            api_base_url=os.getenv("AIHOT_BASE_URL", cls.api_base_url).rstrip("/"),
            connect_timeout_seconds=float(
                os.getenv("AIHOT_CONNECT_TIMEOUT_SECONDS", cls.connect_timeout_seconds)
            ),
            request_timeout_seconds=float(
                os.getenv("AIHOT_REQUEST_TIMEOUT_SECONDS", cls.request_timeout_seconds)
            ),
            max_retries=int(os.getenv("AIHOT_MAX_RETRIES", cls.max_retries)),
            max_pages=int(os.getenv("AIHOT_MAX_PAGES", cls.max_pages)),
        )

