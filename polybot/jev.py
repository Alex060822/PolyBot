from __future__ import annotations

from typing import Any

import httpx

from .config import Settings
from .models import Decision, MarketSnapshot, Side

QUESTIONS: dict[str, Any] = {
    "direction": {
        "type": "choice",
        "instructions": (
            "Which Polymarket outcome is more likely to win this 5-minute Bitcoin window? "
            "Up wins if the Chainlink BTC/USD 60-second TWAP over the window is greater than "
            "or equal to price_to_beat. Down wins otherwise. Use the live spot/TWAP versus "
            "price_to_beat, the remaining time, and whether the Polymarket book already "
            "prices the move. Do not invent prices; use only the state."
        ),
        "criteria": {
            "up": "TWAP at resolution will be >= price_to_beat.",
            "down": "TWAP at resolution will be < price_to_beat.",
        },
    },
    "tradeable": {
        "type": "noul",
        "instructions": (
            "Is there a clear, tradable edge right now versus the current Polymarket ask, "
            "after typical crypto taker fees? Answer no if the book already reflects the "
            "move, if time left is too short, if the signal is noise, or if prices are missing."
        ),
    },
}


class JevError(RuntimeError):
    pass


class DecisionEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._http = httpx.Client(
            timeout=20.0,
            verify=True,
            headers={"User-Agent": "polybot-paper/0.1"},
            follow_redirects=True,
        )
        self._sdk = None
        if settings.uses_jev:
            try:
                from typesafe_sdk import TypeSafeClient

                self._sdk = TypeSafeClient(
                    api_key=settings.typesafe_api_key or None,
                    model=settings.typesafe_model,
                    base_url=settings.typesafe_base_url,
                )
            except Exception:
                self._sdk = None

    def close(self) -> None:
        if self._sdk is not None:
            try:
                self._sdk.close()
            except Exception:
                pass
        self._http.close()

    def decide(self, snapshot: MarketSnapshot) -> Decision:
        if self.settings.decision_backend == "heuristic":
            return heuristic_decision(snapshot)
        if not self.settings.typesafe_api_key:
            raise JevError("TYPESAFE_API_KEY manquante. Ajoute-la dans .env ou passe DECISION_BACKEND=heuristic.")
        raw = self._call_jev(snapshot.to_jev_state())
        return parse_jev_response(raw)

    def _call_jev(self, state: dict[str, Any]) -> dict[str, Any]:
        if self._sdk is not None:
            try:
                from typesafe_sdk import Choice, Noul

                result = self._sdk.system_one(
                    state=state,
                    questions={
                        "direction": Choice(
                            instructions=QUESTIONS["direction"]["instructions"],
                            criteria=QUESTIONS["direction"]["criteria"],
                        ),
                        "tradeable": Noul(instructions=QUESTIONS["tradeable"]["instructions"]),
                    },
                    model=self.settings.typesafe_model,
                )
                direction = result.choices["direction"]
                tradeable = result.nouls["tradeable"]
                return {
                    "answers": {
                        "direction": {
                            "type": "choice",
                            "choice": direction.choice,
                            "confidence": direction.confidence,
                            "probabilities": dict(direction.probabilities),
                        },
                        "tradeable": {"type": "noul", "noul": tradeable.noul},
                    }
                }
            except Exception:
                # HTTP brut si le SDK échoue (Python très récent, etc.)
                pass
        response = self._http.post(
            f"{self.settings.typesafe_base_url}/v1/systemone",
            headers={
                "Authorization": f"Bearer {self.settings.typesafe_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.settings.typesafe_model,
                "state": state,
                "questions": QUESTIONS,
            },
        )
        if response.status_code >= 400:
            raise JevError(f"Jev HTTP {response.status_code}: {response.text[:500]}")
        return response.json()


def parse_jev_response(raw: dict[str, Any]) -> Decision:
    answers = raw.get("answers") or raw
    direction = answers.get("direction") or {}
    tradeable = answers.get("tradeable") or {}
    choice = str(direction.get("choice") or "").lower()
    if choice not in {"up", "down"}:
        raise JevError(f"Choix Jev inattendu: {choice!r}")
    probs = direction.get("probabilities") or {}
    p_up = float(probs.get("up", 1.0 if choice == "up" else 0.0))
    p_down = float(probs.get("down", 1.0 if choice == "down" else 0.0))
    total = p_up + p_down
    if total > 0:
        p_up /= total
        p_down /= total
    return Decision(
        backend="jev",
        side=choice,  # type: ignore[arg-type]
        p_up=p_up,
        p_down=p_down,
        confidence=float(direction.get("confidence") or 0.0),
        tradeable=float(tradeable.get("noul") or 0.0),
        raw=raw,
    )


def heuristic_decision(snapshot: MarketSnapshot) -> Decision:
    """Baseline locale pour tester Polymarket sans clé Jev. Ce n'est pas un edge."""
    delta = snapshot.vs_beat_bps or 0.0
    momentum = snapshot.momentum_1m_bps or 0.0
    score = delta + 0.35 * momentum
    side: Side = "up" if score >= 0 else "down"
    strength = min(abs(score) / 8.0, 0.35)
    p_side = 0.5 + strength
    p_up = p_side if side == "up" else 1.0 - p_side
    confidence = min(0.5 + abs(score) / 20.0, 0.85)
    tradeable = 0.2
    if snapshot.seconds_left >= 40 and abs(score) >= 2:
        tradeable = min(0.55 + abs(score) / 30.0, 0.8)
    return Decision(
        backend="heuristic",
        side=side,
        p_up=p_up,
        p_down=1.0 - p_up,
        confidence=confidence,
        tradeable=tradeable,
        raw={"score_bps": score, "delta_bps": delta, "momentum_bps": momentum},
    )
