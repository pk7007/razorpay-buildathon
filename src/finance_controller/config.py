"""Runtime settings. One place for env + tolerances."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    razorpay_key_id: str = os.getenv("RAZORPAY_KEY_ID", "")
    razorpay_key_secret: str = os.getenv("RAZORPAY_KEY_SECRET", "")
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    llm_model: str = os.getenv("LLM_MODEL", "claude-sonnet-5")

    amount_tolerance_paise: int = int(os.getenv("AMOUNT_TOLERANCE_PAISE", "100"))
    date_tolerance_days: int = int(os.getenv("DATE_TOLERANCE_DAYS", "2"))

    # Minimum confidence to accept an LLM-proposed match group.
    agent_accept_threshold: float = float(os.getenv("AGENT_ACCEPT_THRESHOLD", "0.80"))


SETTINGS = Settings()
