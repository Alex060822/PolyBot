from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Side = Literal["up", "down"]


@dataclass(frozen=True)
class BookLevel:
    price: float
    size: float


@dataclass(frozen=True)
class OutcomeBook:
    token_id: str
    bid: float | None
    ask: float | None
    last_trade: float | None
    bids: tuple[BookLevel, ...] = ()
    asks: tuple[BookLevel, ...] = ()

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid


@dataclass(frozen=True)
class MarketSnapshot:
    slug: str
    title: str
    condition_id: str
    window_start_ts: int
    window_end_ts: int
    seconds_left: float
    price_to_beat: float | None
    accepting_orders: bool
    closed: bool
    resolved_outcome: Side | None
    up: OutcomeBook
    down: OutcomeBook
    btc_spot: float | None
    btc_source: str
    twap_60s: float | None
    vs_beat_bps: float | None
    momentum_1m_bps: float | None
    min_shares: float
    fee_rate: float
    event_url: str

    def book(self, side: Side) -> OutcomeBook:
        return self.up if side == "up" else self.down

    def implied(self, side: Side) -> float | None:
        book = self.book(side)
        if book.ask is not None:
            return book.ask
        return book.mid

    def to_jev_state(self) -> dict[str, Any]:
        return {
            "market": {
                "venue": "polymarket",
                "series": "btc-updown-5m",
                "slug": self.slug,
                "title": self.title,
                "url": self.event_url,
                "seconds_left": round(self.seconds_left, 1),
                "accepting_orders": self.accepting_orders,
                "resolution": (
                    "Up wins if Chainlink BTC/USD 60s TWAP over the 5-minute window "
                    "is greater than or equal to price_to_beat; otherwise Down wins."
                ),
            },
            "bitcoin": {
                "price_to_beat": self.price_to_beat,
                "spot": self.btc_spot,
                "spot_source": self.btc_source,
                "chainlink_twap_60s": self.twap_60s,
                "vs_price_to_beat_bps": self.vs_beat_bps,
                "momentum_1m_bps": self.momentum_1m_bps,
            },
            "polymarket_book": {
                "up": {
                    "bid": self.up.bid,
                    "ask": self.up.ask,
                    "mid": self.up.mid,
                    "spread": self.up.spread,
                    "last_trade": self.up.last_trade,
                },
                "down": {
                    "bid": self.down.bid,
                    "ask": self.down.ask,
                    "mid": self.down.mid,
                    "spread": self.down.spread,
                    "last_trade": self.down.last_trade,
                },
            },
            "fees": {
                "taker_fee_rate": self.fee_rate,
                "formula": "shares * rate * price * (1 - price)",
            },
        }


@dataclass(frozen=True)
class Decision:
    backend: str
    side: Side
    p_up: float
    p_down: float
    confidence: float
    tradeable: float
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def p_side(self) -> float:
        return self.p_up if self.side == "up" else self.p_down


@dataclass(frozen=True)
class Fill:
    side: Side
    shares: float
    avg_price: float
    fee: float
    cost: float
    token_id: str

    @property
    def notional(self) -> float:
        return self.shares * self.avg_price


@dataclass
class Position:
    slug: str
    title: str
    side: Side
    shares: float
    avg_price: float
    fee: float
    cost: float
    token_id: str
    window_end_ts: int
    opened_at: str
    status: str = "open"
    resolved_outcome: Side | None = None
    payout: float | None = None
    pnl: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
