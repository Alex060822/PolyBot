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
        outcome = None
        for _ in range(8):
            outcome = poly.resolved_outcome(position.slug)
            if outcome:
                break
            time.sleep(2)
        if not outcome:
            print(f"  règlement en attente: {position.slug}")
            continue
        settled = account.settle(position.slug, outcome)
        if settled:
            print(
                f"  SETTLED {settled.slug} côté={settled.side} résultat={outcome} "
                f"PnL={settled.pnl:+.2f}$"
            )


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


def run_loop(settings: Settings) -> None:
    ledger = PaperLedger(settings.ledger_path)
    account = PaperAccount(settings.paper_cash, settings.state_path, ledger)
    prices = PriceFeed(settings)
    prices.start()
    poly = PolymarketClient(settings)
    engine = DecisionEngine(settings)
    print("Paper trading BTC Up/Down 5m — aucun ordre réel n'est envoyé.")
    print(account.summary())
    try:
        while True:
            try:
                settle_due(poly, account)
                snapshot = collect_snapshot(poly, prices)
                print()
                print(format_snapshot(snapshot))
                maybe_trade(snapshot, account, engine, settings)
                print(" ", account.summary())
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
