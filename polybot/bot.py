from __future__ import annotations

import json
import time

from .config import Settings
from .jev import DecisionEngine
from .models import Fill, Position
from .netcheck import PolymarketUnreachable
from .paper import PaperAccount, PaperLedger
from .polymarket import PolymarketClient, utc_now_iso
from .prices import PriceFeed
from .strategy import GateResult, evaluate_entry, simulate_taker_buy


def collect_snapshot(poly: PolymarketClient, prices: PriceFeed):
    spot, source, twap, momentum = prices.snapshot()
    return poly.snapshot(
        btc_spot=spot,
        btc_source=source,
        twap_60s=twap,
        momentum_1m_bps=momentum,
    )


def format_snapshot(snapshot) -> str:
    up_ask = f"{snapshot.up.ask:.3f}" if snapshot.up.ask is not None else "?"
    down_ask = f"{snapshot.down.ask:.3f}" if snapshot.down.ask is not None else "?"
    beat = f"{snapshot.price_to_beat:.2f}" if snapshot.price_to_beat else "?"
    spot = f"{snapshot.btc_spot:.2f}" if snapshot.btc_spot else "?"
    vs = f"{snapshot.vs_beat_bps:+.1f} bps" if snapshot.vs_beat_bps is not None else "n/a"
    twap = f"{snapshot.twap_60s:.2f}" if snapshot.twap_60s else "n/a"
    return (
        f"{snapshot.title}\n"
        f"  {snapshot.event_url}\n"
        f"  reste {snapshot.seconds_left:.0f}s | beat={beat} | spot={spot} ({snapshot.btc_source}) | "
        f"twap60={twap} | vs_beat={vs}\n"
        f"  Up ask={up_ask}  Down ask={down_ask}"
    )


def settle_due(poly: PolymarketClient, account: PaperAccount) -> None:
    now = time.time()
    for position in list(account.positions):
        if position.status != "open":
            continue
        if now < position.window_end_ts + 2:
            continue
        outcome = poly.resolved_outcome(position.slug)
        if not outcome:
            print(f"  règlement en attente: {position.slug}")
            continue
        settled = account.settle(position.slug, outcome)
        if settled:
            print(
                f"  SETTLED {settled.slug} côté={settled.side} résultat={outcome} "
                f"PnL={settled.pnl:+.2f}$"
            )


def tours_complete(
    seen_slugs: list[str],
    target: int,
    *,
    last_outcome: str | None,
    positions: list[Position],
) -> bool:
    """True quand le Nième tour a une résolution Gamma et plus aucune position ouverte sur ces tours."""
    if target <= 0 or len(seen_slugs) < target:
        return False
    planned = seen_slugs[:target]
    last = planned[-1]
    last_positions = [pos for pos in positions if pos.slug == last]
    last_resolved = last_outcome is not None or any(pos.status == "settled" for pos in last_positions)
    if not last_resolved:
        return False
    return not any(pos.status == "open" and pos.slug in planned for pos in positions)


def maybe_trade(
    snapshot,
    account: PaperAccount,
    engine: DecisionEngine,
    settings: Settings,
    *,
    record: bool = True,
) -> GateResult | None:
    existing = account.open_on(snapshot.slug)
    if existing:
        print(f"  déjà en position {existing.side.upper()} sur cette fenêtre")
        return None
    if snapshot.seconds_left > settings.max_seconds_left:
        print(
            f"  en attente des 2 dernières minutes "
            f"({snapshot.seconds_left:.0f}s restantes, décision à ≤{settings.max_seconds_left}s)"
        )
        return None
    if snapshot.seconds_left < settings.min_seconds_left:
        print(f"  trop tard ({snapshot.seconds_left:.0f}s restantes), pas de décision")
        return None
    decision = engine.decide(snapshot)
    print(
        f"  {decision.backend}: {decision.side.upper()}  "
        f"p_up={decision.p_up:.2f} p_down={decision.p_down:.2f}  "
        f"conf={decision.confidence:.2f} tradeable={decision.tradeable:.2f}"
    )
    gate = evaluate_entry(snapshot, decision, settings, account.cash)
    extra = {
        "backend": decision.backend,
        "p_up": decision.p_up,
        "p_down": decision.p_down,
        "confidence": decision.confidence,
        "tradeable": decision.tradeable,
        "seconds_left": snapshot.seconds_left,
    }
    if not gate.ok:
        if record:
            account.skip(snapshot.slug, gate.reason, extra)
        print(f"  skip: {gate.reason}")
        return gate
    if not record:
        print(
            f"  signal OK (non enregistré): {gate.side} {gate.shares:.2f} @ {gate.ask:.3f} "
            f"edge={gate.edge:.3f} cost={gate.cost:.2f}$"
        )
        return gate
    book = snapshot.book(gate.side)  # type: ignore[arg-type]
    fill_sim = simulate_taker_buy(book.asks, gate.shares or 0, book.ask or 0, snapshot.fee_rate)
    if fill_sim is None:
        account.skip(snapshot.slug, "fill simulé impossible", extra)
        print("  skip: fill simulé impossible")
        return gate
    fill = Fill(
        side=gate.side,  # type: ignore[arg-type]
        shares=fill_sim.shares,
        avg_price=fill_sim.avg_price,
        fee=fill_sim.fee,
        cost=fill_sim.cost,
        token_id=book.token_id,
    )
    position = Position(
        slug=snapshot.slug,
        title=snapshot.title,
        side=fill.side,
        shares=fill.shares,
        avg_price=fill.avg_price,
        fee=fill.fee,
        cost=fill.cost,
        token_id=fill.token_id,
        window_end_ts=snapshot.window_end_ts,
        opened_at=utc_now_iso(),
    )
    account.fill(position, fill, extra)
    print(
        f"  PAPER BUY {fill.side.upper()} {fill.shares:.2f} @ {fill.avg_price:.3f} "
        f"fee={fill.fee:.4f}$ cost={fill.cost:.2f}$  edge={gate.edge:.3f}"
    )
    return gate


def run_loop(settings: Settings, tours: int = 12) -> None:
    ledger = PaperLedger(settings.ledger_path)
    account = PaperAccount(settings.paper_cash, settings.state_path, ledger)
    prices = PriceFeed(settings)
    prices.start()
    poly = PolymarketClient(settings)
    engine = DecisionEngine(settings)
    seen_slugs: list[str] = []
    print("Paper trading BTC Up/Down 5m — aucun ordre réel n'est envoyé.")
    if tours > 0:
        print(f"{tours} tours de 5 minutes, arrêt après la résolution du dernier.")
    print(account.summary())
    try:
        while True:
            try:
                settle_due(poly, account)
                snapshot = collect_snapshot(poly, prices)
                if snapshot.slug not in seen_slugs and (tours <= 0 or len(seen_slugs) < tours):
                    seen_slugs.append(snapshot.slug)
                    if tours > 0:
                        print(f"\n--- Tour {len(seen_slugs)}/{tours} ---")
                print()
                print(format_snapshot(snapshot))
                planned = snapshot.slug in seen_slugs
                if planned:
                    maybe_trade(snapshot, account, engine, settings)
                elif tours > 0:
                    print(f"  tours terminés, attente de la résolution du tour {tours} ({seen_slugs[-1]})")
                print(" ", account.summary())
                if tours > 0 and seen_slugs:
                    last_slug = seen_slugs[min(len(seen_slugs), tours) - 1]
                    last_outcome = poly.resolved_outcome(last_slug) if len(seen_slugs) >= tours else None
                    if tours_complete(
                        seen_slugs,
                        tours,
                        last_outcome=last_outcome,
                        positions=account.positions,
                    ):
                        print()
                        print(
                            f"Fin : le tour {tours} est résolu"
                            + (f" ({last_outcome})" if last_outcome else "")
                            + "."
                        )
                        print(account.summary())
                        return
            except PolymarketUnreachable as exc:
                print(exc)
                return
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"  erreur cycle: {exc}")
                ledger.append("error", {"error": str(exc)})
            time.sleep(settings.poll_seconds)
    except KeyboardInterrupt:
        print("\nArrêt.")
    finally:
        engine.close()
        poly.close()
        prices.close()


def run_once(settings: Settings, *, decide: bool, trade: bool) -> None:
    ledger = PaperLedger(settings.ledger_path)
    account = PaperAccount(settings.paper_cash, settings.state_path, ledger)
    prices = PriceFeed(settings)
    prices.start()
    time.sleep(1.2)
    poly = PolymarketClient(settings)
    engine = DecisionEngine(settings) if decide else None
    try:
        settle_due(poly, account)
        snapshot = collect_snapshot(poly, prices)
        print(format_snapshot(snapshot))
        print("état marché:")
        print(json.dumps(snapshot.to_jev_state(), indent=2, ensure_ascii=False, default=str))
        if decide and engine is not None:
            maybe_trade(snapshot, account, engine, settings, record=trade)
        print(account.summary())
    except PolymarketUnreachable as exc:
        print(exc)
        raise SystemExit(1) from exc
    finally:
        if engine is not None:
            engine.close()
        poly.close()
        prices.close()
