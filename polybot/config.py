from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


@dataclass(frozen=True)
class Settings:
    typesafe_api_key: str
    typesafe_model: str
    typesafe_base_url: str
    decision_backend: str
    paper_cash: float
    stake_usd: float
    max_bankroll_fraction: float
    min_confidence: float
    min_edge: float
    min_tradeable: float
    min_seconds_left: int
    max_seconds_left: int
    max_spread: float
    min_ask: float
    max_ask: float
    poll_seconds: float
    ssl_verify: bool | str
    asset: str = "btc"
    timeframe: str = "5m"
    window_seconds: int = 300
    crypto_taker_fee_rate: float = 0.07
    min_shares: float = 5.0
    gamma_url: str = "https://gamma-api.polymarket.com"
    clob_url: str = "https://clob.polymarket.com"
    rtds_url: str = "wss://ws-live-data.polymarket.com"
    ledger_path: Path = DATA_DIR / "paper_ledger.jsonl"
    state_path: Path = DATA_DIR / "paper_state.json"

    @property
    def uses_jev(self) -> bool:
        return self.decision_backend == "jev"


def load_settings() -> Settings:
    load_dotenv(ROOT / ".env")
    backend = os.getenv("DECISION_BACKEND", "jev").strip().lower()
    if backend not in {"jev", "heuristic"}:
        raise ValueError("DECISION_BACKEND must be 'jev' or 'heuristic'")
    verify_raw = os.getenv("POLYMARKET_SSL_VERIFY", "true").strip().lower()
    ssl_verify: bool | str
    if verify_raw in {"0", "false", "no", "off"}:
        ssl_verify = False
    else:
        try:
            import certifi

            ssl_verify = certifi.where()
        except ImportError:
            ssl_verify = True
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return Settings(
        typesafe_api_key=os.getenv("TYPESAFE_API_KEY", "").strip(),
        typesafe_model=os.getenv("TYPESAFE_DEFAULT_MODEL", "jev-latest").strip(),
        typesafe_base_url=os.getenv("TYPESAFE_BASE_URL", "https://api.typesafe.ai").rstrip("/"),
        decision_backend=backend,
        paper_cash=_float("PAPER_CASH", 1000.0),
        stake_usd=_float("STAKE_USD", 15.0),
        max_bankroll_fraction=_float("MAX_BANKROLL_FRACTION", 0.1),
        min_confidence=_float("MIN_CONFIDENCE", 0.58),
        min_edge=_float("MIN_EDGE", 0.02),
        min_tradeable=_float("MIN_TRADEABLE", 0.0),
        min_seconds_left=_int("MIN_SECONDS_LEFT", 25),
        max_seconds_left=_int("MAX_SECONDS_LEFT", 120),
        max_spread=_float("MAX_SPREAD", 0.08),
        min_ask=_float("MIN_ASK", 0.08),
        max_ask=_float("MAX_ASK", 0.92),
        poll_seconds=_float("POLL_SECONDS", 8.0),
        ssl_verify=ssl_verify,
    )
