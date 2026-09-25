from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import Fill, Position, Side
from .polymarket import utc_now_iso


class PaperLedger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, kind: str, payload: dict[str, Any]) -> None:
        row = {"ts": utc_now_iso(), "kind": kind, **payload}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


class PaperAccount:
    def __init__(self, cash: float, state_path: Path, ledger: PaperLedger) -> None:
        self.state_path = state_path
        self.ledger = ledger
        self.cash = cash
        self.realized_pnl = 0.0
        self.wins = 0
        self.losses = 0
        self.skips = 0
        self.positions: list[Position] = []
        self._load()

    def _load(self) -> None:
        if not self.state_path.exists():
            self._save()
            return
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.cash = float(data.get("cash", self.cash))
        self.realized_pnl = float(data.get("realized_pnl", 0.0))
        self.wins = int(data.get("wins", 0))
        self.losses = int(data.get("losses", 0))
        self.skips = int(data.get("skips", 0))
        self.positions = [Position(**item) for item in data.get("positions", [])]

    def _save(self) -> None:
        payload = {
            "cash": self.cash,
            "realized_pnl": self.realized_pnl,
            "wins": self.wins,
            "losses": self.losses,
            "skips": self.skips,
            "open_positions": sum(1 for pos in self.positions if pos.status == "open"),
            "positions": [pos.to_dict() for pos in self.positions],
        }
        self.state_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def open_on(self, slug: str) -> Position | None:
        for pos in self.positions:
            if pos.slug == slug and pos.status == "open":
                return pos
        return None

    def skip(self, slug: str, reason: str, extra: dict[str, Any] | None = None) -> None:
        self.skips += 1
        self.ledger.append("skip", {"slug": slug, "reason": reason, **(extra or {})})
        self._save()

    def fill(self, position: Position, fill: Fill, extra: dict[str, Any] | None = None) -> Position:
        if fill.cost > self.cash + 1e-9:
            raise ValueError("Cash paper insuffisant")
        self.cash -= fill.cost
        self.positions.append(position)
        self.ledger.append(
            "fill",
            {
                "slug": position.slug,
                "side": fill.side,
                "shares": fill.shares,
                "avg_price": fill.avg_price,
                "fee": fill.fee,
                "cost": fill.cost,
                "cash_after": self.cash,
                **(extra or {}),
            },
        )
        self._save()
        return position

    def settle(self, slug: str, outcome: Side) -> Position | None:
        position = self.open_on(slug)
        if position is None:
            return None
        won = position.side == outcome
        payout = position.shares if won else 0.0
        pnl = payout - position.cost
        self.cash += payout
        self.realized_pnl += pnl
        if won:
            self.wins += 1
        else:
            self.losses += 1
        position.status = "settled"
        position.resolved_outcome = outcome
        position.payout = payout
        position.pnl = pnl
        self.ledger.append(
            "settle",
            {
                "slug": slug,
                "side": position.side,
                "outcome": outcome,
                "payout": payout,
                "pnl": pnl,
                "cash_after": self.cash,
                "realized_pnl": self.realized_pnl,
            },
        )
        self._save()
        return position

    def summary(self) -> str:
        open_n = sum(1 for pos in self.positions if pos.status == "open")
        trades = self.wins + self.losses
        wr = (self.wins / trades * 100) if trades else 0.0
        return (
            f"cash={self.cash:.2f}$  PnL={self.realized_pnl:+.2f}$  "
            f"W/L={self.wins}/{self.losses} ({wr:.0f}%)  open={open_n}  skips={self.skips}"
        )
