"""Runtime settings. One place for env + tolerances."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    razorpay_key_id: str = os.getenv("RAZORPAY_KEY_ID", "")
    razorpay_key_secret: str = os.getenv("RAZORPAY_KEY_SECRET", "")

    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    # Optional shared secret. Unset (the default) leaves the service open, which
    # is what a local demo wants. Set it and every state-changing request must
    # present it -- see `requires_token`.
    api_token: str = os.getenv("RECON_API_TOKEN", "")
    llm_model: str = os.getenv("LLM_MODEL", "claude-sonnet-5")
    # USD per 1M tokens — override if you point at a different model.
    llm_input_usd_per_mtok: float = _float("LLM_INPUT_USD_PER_MTOK", 3.0)
    llm_output_usd_per_mtok: float = _float("LLM_OUTPUT_USD_PER_MTOK", 15.0)
    # a reconciliation run must stay interactive: bound the model, then fall back
    llm_timeout_seconds: float = _float("LLM_TIMEOUT_SECONDS", 20.0)
    llm_max_retries: int = _int("LLM_MAX_RETRIES", 2)

    amount_tolerance_paise: int = _int("AMOUNT_TOLERANCE_PAISE", 100)   # 1.00 rupee
    date_tolerance_days: int = _int("DATE_TOLERANCE_DAYS", 3)
    settlement_lag_days: int = _int("SETTLEMENT_LAG_DAYS", 2)           # Razorpay T+2
    # payouts can be held, batched weekly, or cross a period boundary; beyond
    # this many days a match needs more than an amount to be believable
    max_payout_lag_days: int = _int("MAX_PAYOUT_LAG_DAYS", 60)

    # Minimum confidence to accept a resolver-proposed match group.
    resolver_accept_threshold: float = _float("RESOLVER_ACCEPT_THRESHOLD", 0.72)

    @property
    def has_llm(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def requires_token(self) -> bool:
        return bool(self.api_token)

    @property
    def has_razorpay(self) -> bool:
        return bool(self.razorpay_key_id and self.razorpay_key_secret)


SETTINGS = Settings()
