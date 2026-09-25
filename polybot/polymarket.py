from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from .config import Settings
from .models import BookLevel, MarketSnapshot, OutcomeBook, Side
from .netcheck import wrap_http_error


def current_window_start(now: float | None = None, window: int = 300) -> int:
    ts = int(now if now is not None else time.time())
    return (ts // window) * window


def event_slug(asset: str, timeframe: str, start_ts: int) -> str:
    return f"{asset}-updown-{timeframe}-{start_ts}"


def _parse_json_field(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def taker_fee(shares: float, price: float, rate: float) -> float:
    """Polymarket crypto taker fee: shares * rate * p * (1-p)."""
    p = min(max(price, 0.0), 1.0)
    return round(shares * rate * p * (1.0 - p), 5)


def walk_asks(asks: tuple[BookLevel, ...], shares: float) -> tuple[float, float]:
    remaining = shares
    cost = 0.0
    filled = 0.0
    for level in sorted(asks, key=lambda item: item.price):
        take = min(remaining, level.size)
        if take <= 0:
            continue
        cost += take * level.price
        filled += take
        remaining -= take
        if remaining <= 1e-9:
            break
    return filled, cost


def iso_to_ts(value: str | None) -> int | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    return int(datetime.fromisoformat(text).timestamp())


class PolymarketClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._http = httpx.Client(
            timeout=12.0,
            verify=settings.ssl_verify,
            headers={"User-Agent": "polybot-paper/0.1"},
            follow_redirects=True,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> PolymarketClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get_event(self, slug: str) -> dict[str, Any] | None:
        try:
            response = self._http.get(
                f"{self.settings.gamma_url}/events",
                params={"slug": slug},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise wrap_http_error(exc, "gamma-api.polymarket.com") from exc
        payload = response.json()
        if not payload:
            return None
        if isinstance(payload, list):
            return payload[0]
        return payload

    def find_live_event(self, now: float | None = None) -> tuple[str, dict[str, Any]]:
        start = current_window_start(now, self.settings.window_seconds)
        candidates = [start, start + self.settings.window_seconds, start - self.settings.window_seconds]
        last_error: Exception | None = None
        for ts in candidates:
            slug = event_slug(self.settings.asset, self.settings.timeframe, ts)
            try:
                event = self.get_event(slug)
            except httpx.HTTPError as exc:
                last_error = exc
                continue
            if event:
                return slug, event
        if last_error:
            raise last_error
        raise LookupError("Aucun marché BTC 5m trouvé via Gamma pour la fenêtre courante.")

    def get_book(self, token_id: str) -> dict[str, Any] | None:
        try:
            response = self._http.get(
                f"{self.settings.clob_url}/book",
                params={"token_id": token_id},
            )
        except httpx.HTTPError as exc:
            raise wrap_http_error(exc, "clob.polymarket.com") from exc
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def get_price(self, token_id: str, side: str) -> float | None:
        try:
            response = self._http.get(
                f"{self.settings.clob_url}/price",
                params={"token_id": token_id, "side": side},
            )
        except httpx.HTTPError as exc:
            raise wrap_http_error(exc, "clob.polymarket.com") from exc
        if response.status_code in {400, 404}:
            return None
        response.raise_for_status()
        payload = response.json()
        return _float_or_none(payload.get("price") or payload.get("mid") or payload.get("mid_price"))

    def outcome_book(self, token_id: str, gamma_bid: float | None, gamma_ask: float | None, last_trade: float | None) -> OutcomeBook:
        raw = self.get_book(token_id)
        bids: list[BookLevel] = []
        asks: list[BookLevel] = []
        if raw:
            for row in raw.get("bids") or []:
                price = _float_or_none(row.get("price"))
                size = _float_or_none(row.get("size"))
                if price is not None and size is not None:
                    bids.append(BookLevel(price, size))
            for row in raw.get("asks") or []:
                price = _float_or_none(row.get("price"))
                size = _float_or_none(row.get("size"))
                if price is not None and size is not None:
                    asks.append(BookLevel(price, size))
            last_trade = _float_or_none(raw.get("last_trade_price")) or last_trade
        bid = max((level.price for level in bids), default=None)
        ask = min((level.price for level in asks), default=None)
        if bid is None:
            bid = self.get_price(token_id, "sell") or gamma_bid
        if ask is None:
            ask = self.get_price(token_id, "buy") or gamma_ask
        return OutcomeBook(
            token_id=token_id,
            bid=bid,
            ask=ask,
            last_trade=last_trade,
            bids=tuple(sorted(bids, key=lambda item: item.price, reverse=True)),
            asks=tuple(sorted(asks, key=lambda item: item.price)),
        )

    def snapshot(
        self,
        *,
        btc_spot: float | None,
        btc_source: str,
        twap_60s: float | None,
        momentum_1m_bps: float | None,
        now: float | None = None,
    ) -> MarketSnapshot:
        slug, event = self.find_live_event(now)
        markets = event.get("markets") or []
        if not markets:
            raise LookupError(f"Événement {slug} sans marché.")
        market = markets[0]
        outcomes = [str(item).lower() for item in _parse_json_field(market.get("outcomes")) or []]
        token_ids = [str(item) for item in _parse_json_field(market.get("clobTokenIds")) or []]
        if "up" not in outcomes or "down" not in outcomes or len(token_ids) < 2:
            raise ValueError(f"Outcomes inattendus pour {slug}: {outcomes}")
        up_id = token_ids[outcomes.index("up")]
        down_id = token_ids[outcomes.index("down")]
        metadata = event.get("eventMetadata") or {}
        price_to_beat = _float_or_none(metadata.get("priceToBeat"))
        start_ts = iso_to_ts(event.get("startTime") or market.get("eventStartTime")) or current_window_start(now)
        end_ts = iso_to_ts(event.get("endDate") or market.get("endDate")) or (start_ts + self.settings.window_seconds)
        clock = now if now is not None else time.time()
        up = self.outcome_book(
            up_id,
            _float_or_none(market.get("bestBid")),
            _float_or_none(market.get("bestAsk")),
            _float_or_none(market.get("lastTradePrice")),
        )
        down = self.outcome_book(down_id, None, None, None)
        ref = twap_60s or btc_spot
        vs_beat_bps = None
        if ref is not None and price_to_beat:
            vs_beat_bps = (ref - price_to_beat) / price_to_beat * 10_000
        fee_rate = _float_or_none((market.get("feeSchedule") or {}).get("rate")) or self.settings.crypto_taker_fee_rate
        return MarketSnapshot(
            slug=slug,
            title=str(event.get("title") or market.get("question") or slug),
            condition_id=str(market.get("conditionId") or ""),
            window_start_ts=start_ts,
            window_end_ts=end_ts,
            seconds_left=max(0.0, end_ts - clock),
            price_to_beat=price_to_beat,
            accepting_orders=bool(market.get("acceptingOrders", True)),
            closed=bool(event.get("closed") or market.get("closed")),
            resolved_outcome=resolved_side(market),
            up=up,
            down=down,
            btc_spot=btc_spot,
            btc_source=btc_source,
            twap_60s=twap_60s,
            vs_beat_bps=vs_beat_bps,
            momentum_1m_bps=momentum_1m_bps,
            min_shares=_float_or_none(market.get("orderMinSize")) or self.settings.min_shares,
            fee_rate=fee_rate,
            event_url=f"https://polymarket.com/event/{slug}",
        )

    def resolved_outcome(self, slug: str) -> Side | None:
        event = self.get_event(slug)
        if not event:
            return None
        markets = event.get("markets") or []
        if not markets:
            return None
        return resolved_side(markets[0])


def resolved_side(market: dict[str, Any]) -> Side | None:
    outcomes = [str(item).lower() for item in _parse_json_field(market.get("outcomes")) or []]
    prices = _parse_json_field(market.get("outcomePrices")) or []
    if len(outcomes) != len(prices) or not outcomes:
        return None
    winners = [name for name, price in zip(outcomes, prices, strict=False) if _float_or_none(price) == 1.0]
    if len(winners) != 1:
        return None
    winner = winners[0]
    if winner in {"up", "down"}:
        return winner  # type: ignore[return-value]
    return None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
