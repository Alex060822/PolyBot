from __future__ import annotations

import json
import threading
import time
from typing import Any

import httpx

from .config import Settings


class PriceFeed:
    """Spot BTC (Binance/Coinbase) + TWAP Chainlink 60s via RTDS Polymarket si dispo."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._http = httpx.Client(
            timeout=8.0,
            verify=settings.ssl_verify,
            headers={"User-Agent": "polybot-paper/0.1"},
            follow_redirects=True,
        )
        self._twap: float | None = None
        self._twap_ts: float = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run_twap_loop, name="chainlink-twap", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._http.close()

    def snapshot(self) -> tuple[float | None, str, float | None, float | None]:
        spot, source = self._spot()
        momentum = self._momentum_1m_bps()
        twap = self._twap if (time.time() - self._twap_ts) < 30 else None
        return spot, source, twap, momentum

    def _spot(self) -> tuple[float | None, str]:
        try:
            payload = self._http.get("https://api.binance.com/api/v3/ticker/price", params={"symbol": "BTCUSDT"}).json()
            return float(payload["price"]), "binance"
        except Exception:
            pass
        try:
            payload = self._http.get("https://api.coinbase.com/v2/prices/BTC-USD/spot").json()
            return float(payload["data"]["amount"]), "coinbase"
        except Exception:
            return None, "none"

    def _momentum_1m_bps(self) -> float | None:
        try:
            rows = self._http.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol": "BTCUSDT", "interval": "1m", "limit": 2},
            ).json()
            prev_close = float(rows[0][4])
            last_close = float(rows[-1][4])
            if prev_close <= 0:
                return None
            return (last_close - prev_close) / prev_close * 10_000
        except Exception:
            return None

    def _run_twap_loop(self) -> None:
        try:
            import websockets
        except ImportError:
            return
        while not self._stop.is_set():
            try:
                self._listen(websockets)
            except Exception:
                self._stop.wait(5)

    def _listen(self, websockets: Any) -> None:
        subscribe = {
            "action": "subscribe",
            "subscriptions": [
                {
                    "topic": "crypto_prices_twap_sixty",
                    "type": "update",
                    "filters": '{"symbol":"btc/usd"}',
                }
            ],
        }
        with websockets.sync.client.connect(
            self.settings.rtds_url,
            open_timeout=10,
            close_timeout=3,
        ) as ws:
            ws.send(json.dumps(subscribe))
            last_ping = time.time()
            while not self._stop.is_set():
                if time.time() - last_ping >= 5:
                    ws.send("PING")
                    last_ping = time.time()
                try:
                    message = ws.recv(timeout=1)
                except TimeoutError:
                    continue
                if not message or message in {"PONG", "PING"}:
                    continue
                self._handle_twap_message(message)

    def _handle_twap_message(self, message: str) -> None:
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            return
        body = payload.get("payload") or payload
        symbol = str(body.get("symbol") or "").lower()
        if symbol and symbol not in {"btc/usd", "btc-usd", "btcusd"}:
            return
        value = body.get("value")
        if value is None:
            return
        try:
            self._twap = float(value)
            self._twap_ts = time.time()
        except (TypeError, ValueError):
            return
