from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .models import BookLevel, Decision, Fill, MarketSnapshot, Side
from .polymarket import taker_fee, walk_asks


@dataclass(frozen=True)
class GateResult:
    ok: bool
    reason: str
    side: Side | None = None
    edge: float | None = None
    ask: float | None = None
    fee: float | None = None
    shares: float | None = None
    cost: float | None = None


def expected_edge(p_model: float, ask: float, fee_rate: float) -> float:
    fee_per_share = taker_fee(1.0, ask, fee_rate)
    return p_model - ask - fee_per_share


def simulate_taker_buy(asks: tuple[BookLevel, ...], shares: float, fallback_ask: float, fee_rate: float) -> Fill | None:
    filled, notional = walk_asks(asks, shares)
    if filled + 1e-9 < shares:
        if fallback_ask <= 0:
            return None
        filled = shares
        notional = shares * fallback_ask
    avg = notional / filled
    fee = taker_fee(filled, avg, fee_rate)
    return Fill(
        side="up",
        shares=round(filled, 4),
        avg_price=round(avg, 6),
        fee=fee,
        cost=round(notional + fee, 6),
        token_id="",
    )


def evaluate_entry(
    snapshot: MarketSnapshot,
    decision: Decision,
    settings: Settings,
    cash: float,
) -> GateResult:
    if snapshot.closed:
        return GateResult(False, "marché déjà clos")
    if not snapshot.accepting_orders:
        return GateResult(False, "carnet fermé")
    if snapshot.seconds_left < settings.min_seconds_left:
        return GateResult(False, f"trop tard ({snapshot.seconds_left:.0f}s restantes)")
    if snapshot.seconds_left > settings.max_seconds_left:
        return GateResult(False, f"trop tôt ({snapshot.seconds_left:.0f}s restantes)")
    if decision.confidence < settings.min_confidence:
        return GateResult(False, f"confiance trop basse ({decision.confidence:.2f})")
    if settings.min_tradeable > 0 and decision.tradeable < settings.min_tradeable:
        return GateResult(False, f"Jev ne voit pas d'edge tradable ({decision.tradeable:.2f})")

    side = decision.side
    book = snapshot.book(side)
    if book.ask is None:
        return GateResult(False, f"pas d'ask {side}")
    if book.ask < settings.min_ask or book.ask > settings.max_ask:
        return GateResult(False, f"ask {side} hors bande ({book.ask:.3f})")
    spread = book.spread
    if spread is not None and spread > settings.max_spread:
        return GateResult(False, f"spread trop large ({spread:.3f})")

    edge = expected_edge(decision.p_side, book.ask, snapshot.fee_rate)
    if edge < settings.min_edge:
        return GateResult(False, f"edge insuffisant ({edge:.3f} < {settings.min_edge:.3f})", side, edge, book.ask)

    stake = min(settings.stake_usd, cash * settings.max_bankroll_fraction, cash)
    shares = max(snapshot.min_shares, round(stake / book.ask, 2))
    fill = simulate_taker_buy(book.asks, shares, book.ask, snapshot.fee_rate)
    if fill is None or fill.cost <= 0:
        return GateResult(False, "impossible de simuler le fill")
    if fill.cost > cash:
        return GateResult(False, f"cash insuffisant ({cash:.2f}$ < {fill.cost:.2f}$)")
    fill = Fill(
        side=side,
        shares=fill.shares,
        avg_price=fill.avg_price,
        fee=fill.fee,
        cost=fill.cost,
        token_id=book.token_id,
    )
    return GateResult(
        True,
        "ok",
        side=side,
        edge=edge,
        ask=fill.avg_price,
        fee=fill.fee,
        shares=fill.shares,
        cost=fill.cost,
    )
