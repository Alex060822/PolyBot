from __future__ import annotations

from pathlib import Path

from polybot.config import Settings
from polybot.jev import heuristic_decision, parse_jev_response
from polybot.models import BookLevel, Decision, Fill, MarketSnapshot, OutcomeBook, Position
from polybot.paper import PaperAccount, PaperLedger
from polybot.polymarket import current_window_start, event_slug, resolved_side, taker_fee, walk_asks
from polybot.strategy import evaluate_entry, expected_edge, simulate_taker_buy


def test_window_slug() -> None:
    assert current_window_start(1_789_919_123, 300) == 1_789_919_100
    assert event_slug("btc", "5m", 1_789_919_100) == "btc-updown-5m-1789919100"


def test_crypto_taker_fee_matches_docs() -> None:
    assert taker_fee(100, 0.50, 0.07) == 1.75
    assert taker_fee(100, 0.30, 0.07) == 1.47


def test_walk_asks() -> None:
    asks = (BookLevel(0.40, 2), BookLevel(0.41, 10))
    filled, cost = walk_asks(asks, 5)
    assert filled == 5
    assert round(cost, 4) == 2.03


def test_resolved_side() -> None:
    market = {"outcomes": '["Up", "Down"]', "outcomePrices": '["1", "0"]'}
    assert resolved_side(market) == "up"
    market["outcomePrices"] = '["0.6", "0.4"]'
    assert resolved_side(market) is None


def _snapshot(**overrides) -> MarketSnapshot:
    up = OutcomeBook("tok-up", bid=0.49, ask=0.51, last_trade=0.50)
    down = OutcomeBook("tok-down", bid=0.48, ask=0.50, last_trade=0.49)
    base = dict(
        slug="btc-updown-5m-1",
        title="Bitcoin Up or Down",
        condition_id="0xabc",
        window_start_ts=1,
        window_end_ts=301,
        seconds_left=120,
        price_to_beat=80000.0,
        accepting_orders=True,
        closed=False,
        resolved_outcome=None,
        up=up,
        down=down,
        btc_spot=80020.0,
        btc_source="binance",
        twap_60s=80010.0,
        vs_beat_bps=2.5,
        momentum_1m_bps=1.0,
        min_shares=5.0,
        fee_rate=0.07,
        event_url="https://polymarket.com/event/btc-updown-5m-1",
    )
    base.update(overrides)
    return MarketSnapshot(**base)


def _settings(**overrides) -> Settings:
    values = dict(
        typesafe_api_key="",
        typesafe_model="jev-latest",
        typesafe_base_url="https://api.typesafe.ai",
        decision_backend="heuristic",
        paper_cash=1000,
        stake_usd=15,
        max_bankroll_fraction=0.1,
        min_confidence=0.50,
        min_edge=0.03,
        min_tradeable=0.50,
        min_seconds_left=25,
        max_seconds_left=270,
        max_spread=0.08,
        min_ask=0.08,
        max_ask=0.92,
        poll_seconds=8,
        ssl_verify=True,
    )
    values.update(overrides)
    return Settings(**values)


def test_parse_jev_response() -> None:
    decision = parse_jev_response(
        {
            "answers": {
                "direction": {
                    "choice": "up",
                    "confidence": 0.72,
                    "probabilities": {"up": 0.64, "down": 0.36},
                },
                "tradeable": {"noul": 0.61},
            }
        }
    )
    assert decision.side == "up"
    assert decision.p_up == 0.64
    assert decision.confidence == 0.72


def test_gate_rejects_low_edge() -> None:
    snapshot = _snapshot()
    decision = Decision("jev", "up", 0.52, 0.48, 0.80, 0.80)
    gate = evaluate_entry(snapshot, decision, _settings(), cash=1000)
    assert not gate.ok
    assert "edge" in gate.reason


def test_gate_accepts_clear_edge() -> None:
    snapshot = _snapshot()
    decision = Decision("jev", "up", 0.70, 0.30, 0.80, 0.80)
    gate = evaluate_entry(snapshot, decision, _settings(), cash=1000)
    assert gate.ok
    assert gate.side == "up"
    assert gate.cost and gate.cost > 0
    assert expected_edge(0.70, 0.51, 0.07) > 0.03


def test_paper_fill_and_settle(tmp_path: Path) -> None:
    ledger = PaperLedger(tmp_path / "ledger.jsonl")
    account = PaperAccount(100.0, tmp_path / "state.json", ledger)
    simulated = simulate_taker_buy((BookLevel(0.40, 20),), 10, 0.40, 0.07)
    assert simulated is not None
    fill = Fill(
        side="up",
        shares=simulated.shares,
        avg_price=simulated.avg_price,
        fee=simulated.fee,
        cost=simulated.cost,
        token_id="tok-up",
    )
    position = Position(
        slug="btc-updown-5m-1",
        title="t",
        side="up",
        shares=fill.shares,
        avg_price=fill.avg_price,
        fee=fill.fee,
        cost=fill.cost,
        token_id=fill.token_id,
        window_end_ts=1,
        opened_at="2026-09-20T00:00:00+00:00",
    )
    account.fill(position, fill)
    assert account.cash == 100 - fill.cost
    settled = account.settle("btc-updown-5m-1", "up")
    assert settled is not None
    assert settled.pnl is not None
    assert account.wins == 1
    assert abs(account.cash - (100 - fill.cost + fill.shares)) < 1e-9


def test_heuristic_picks_a_side() -> None:
    decision = heuristic_decision(_snapshot(vs_beat_bps=12.0, momentum_1m_bps=4.0))
    assert decision.side == "up"
    assert decision.p_up > 0.5
