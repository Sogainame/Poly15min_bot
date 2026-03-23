"""Polymarket API layer for 15-minute straddle bot.

Based on Sniper V3 market.py, adapted for:
- 15m market slugs (btc-updown-15m-{ts}, divisible by 900)
- Buying BOTH sides (UP + DOWN)
- Selling via GTC maker orders
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

import config
from notifier import send_telegram

CLOB_HOST = config.CLOB_HOST
GAMMA_API = config.GAMMA_API
MAX_ORDER_RETRIES = 2
RETRY_DELAY = 0.5

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import (
        ApiCreds, AssetType, BalanceAllowanceParams,
        OrderArgs, OrderType,
    )
    from py_clob_client.order_builder.constants import BUY, SELL
except Exception:
    ClobClient = None  # type: ignore[assignment]
    ApiCreds = None  # type: ignore[assignment]
    AssetType = None  # type: ignore[assignment]
    BalanceAllowanceParams = None  # type: ignore[assignment]
    OrderArgs = None  # type: ignore[assignment]
    BUY = "BUY"
    SELL = "SELL"

    class OrderType:  # type: ignore[no-redef]
        GTC = "GTC"
        FOK = "FOK"


@dataclass
class Book:
    best_bid: float = 0.0
    best_ask: float = 0.0
    spread: float = 0.0


class PolymarketClient:
    def __init__(self) -> None:
        self.http = httpx.Client(timeout=config.HTTP_TIMEOUT)
        self._api_key = ""
        self._api_secret = ""
        self._api_passphrase = ""
        self.clob = self._init_clob()

    @staticmethod
    def _to_float(value: Any) -> float:
        try:
            return float(value)
        except Exception:
            return 0.0

    # ── CLOB Init ────────────────────────────────────────────────────────

    def _init_clob(self) -> Any | None:
        if ClobClient is None or not config.POLY_PRIVATE_KEY:
            return None
        try:
            kwargs: dict[str, Any] = {
                "host": CLOB_HOST,
                "chain_id": config.CHAIN_ID,
                "key": config.POLY_PRIVATE_KEY,
                "signature_type": config.POLY_SIGNATURE_TYPE,
            }
            if config.POLY_FUNDER_ADDRESS:
                kwargs["funder"] = config.POLY_FUNDER_ADDRESS

            if ApiCreds and config.POLY_API_KEY and config.POLY_API_SECRET and config.POLY_API_PASSPHRASE:
                kwargs["creds"] = ApiCreds(
                    api_key=config.POLY_API_KEY,
                    api_secret=config.POLY_API_SECRET,
                    api_passphrase=config.POLY_API_PASSPHRASE,
                )
                self._api_key = config.POLY_API_KEY
                self._api_secret = config.POLY_API_SECRET
                self._api_passphrase = config.POLY_API_PASSPHRASE
                return ClobClient(**kwargs)

            client = ClobClient(**kwargs)
            try:
                creds = client.create_or_derive_api_creds()
                if creds:
                    client.set_api_creds(creds)
                    self._api_key = getattr(creds, "api_key", "")
                    self._api_secret = getattr(creds, "api_secret", "")
                    self._api_passphrase = getattr(creds, "api_passphrase", "")
            except Exception as e:
                print(f"[!] CLOB creds warning: {e}")
            return client
        except Exception as exc:
            print(f"[!] CLOB init warning: {exc}")
            return None

    # ── Balance ──────────────────────────────────────────────────────────

    def get_balance(self) -> float | None:
        if self.clob is None or BalanceAllowanceParams is None or AssetType is None:
            return None
        try:
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            resp = self.clob.get_balance_allowance(params)
            if isinstance(resp, dict):
                raw = self._to_float(resp.get("balance", 0))
                bal = raw / 1e6 if raw > 10_000 else raw
                if bal > 0.01:
                    return bal
        except Exception:
            pass

        if self._api_key:
            try:
                headers = {
                    "POLY_API_KEY": self._api_key,
                    "POLY_API_SECRET": self._api_secret,
                    "POLY_PASSPHRASE": self._api_passphrase,
                }
                r = self.http.get(
                    f"{CLOB_HOST}/balance-allowance",
                    params={"asset_type": "COLLATERAL", "signature_type": "1"},
                    headers=headers,
                    timeout=10.0,
                )
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, dict):
                        raw = self._to_float(data.get("balance", 0))
                        bal = raw / 1e6 if raw > 10_000 else raw
                        if bal > 0.01:
                            return bal
            except Exception:
                pass
        return None

    # ── Token Prices ─────────────────────────────────────────────────────

    def fetch_midpoint(self, token_id: str) -> float:
        try:
            resp = self.http.get(
                f"{CLOB_HOST}/midpoint",
                params={"token_id": token_id},
                timeout=5.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                mid = self._to_float(data.get("mid", 0))
                if mid > 0.01:
                    return mid
                mid = self._to_float(data.get("mid_price", 0))
                if mid > 0.01:
                    return mid
        except Exception:
            pass
        return 0.0

    def _fetch_best_price(self, token_id: str, side: str) -> float:
        try:
            resp = self.http.get(
                f"{CLOB_HOST}/price",
                params={"token_id": token_id, "side": side},
                timeout=5.0,
            )
            if resp.status_code == 200:
                return self._to_float(resp.json().get("price", 0))
        except Exception:
            pass
        return 0.0

    def fetch_book(self, token_id: str) -> Book:
        bid = self._fetch_best_price(token_id, "SELL")
        ask = self._fetch_best_price(token_id, "BUY")
        if bid <= 0 or ask <= 0:
            return Book()
        return Book(best_bid=bid, best_ask=ask, spread=max(ask - bid, 0.0))

    # ── Market Lookup (15-minute) ────────────────────────────────────────

    def _load_market(self, slug: str) -> dict[str, Any] | None:
        try:
            resp = self.http.get(
                f"{GAMMA_API}/markets",
                params={"slug": slug},
                timeout=10.0,
            )
            if resp.status_code != 200:
                return None
            payload = resp.json()
            markets = payload if isinstance(payload, list) else [payload]
            return next((m for m in markets if m and m.get("slug") == slug), None)
        except Exception:
            return None

    def find_15m_market(self, window_ts: int) -> dict[str, Any] | None:
        """Find BTC 15-minute market for given window timestamp."""
        slug = f"btc-updown-15m-{window_ts}"
        market = self._load_market(slug)
        if not market:
            return None

        clob_ids = market.get("clobTokenIds", [])
        if isinstance(clob_ids, str):
            try:
                clob_ids = json.loads(clob_ids)
            except Exception:
                clob_ids = []
        outcomes = market.get("outcomes", [])
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except Exception:
                outcomes = []

        up_token = clob_ids[0] if len(clob_ids) >= 1 else ""
        down_token = clob_ids[1] if len(clob_ids) >= 2 else ""
        for i, name in enumerate(outcomes):
            if i >= len(clob_ids):
                break
            lname = str(name).lower()
            if lname in {"up", "yes"}:
                up_token = clob_ids[i]
            elif lname in {"down", "no"}:
                down_token = clob_ids[i]

        return {
            "slug": slug,
            "condition_id": market.get("conditionId", ""),
            "up_token": up_token,
            "down_token": down_token,
        }

    def get_market_resolution(self, slug: str) -> str | None:
        """Check Gamma API for market resolution winner."""
        market = self._load_market(slug)
        if not market:
            return None

        tokens = market.get("tokens") or []
        if isinstance(tokens, str):
            try:
                tokens = json.loads(tokens)
            except Exception:
                tokens = []
        for token in tokens:
            if bool(token.get("winner", False)):
                outcome = str(token.get("outcome", "")).lower()
                if outcome in {"yes", "up"}:
                    return "UP"
                if outcome in {"no", "down"}:
                    return "DOWN"

        winner = str(
            market.get("winningOutcome", "") or market.get("winner", "")
        ).lower()
        if winner in {"yes", "up"}:
            return "UP"
        if winner in {"no", "down"}:
            return "DOWN"
        return None

    # ── Order Execution ──────────────────────────────────────────────────

    def submit_maker_buy(
        self, token_id: str, price: float, size: float, label: str
    ) -> str | None:
        if self.clob is None or OrderArgs is None:
            print(f"[!] Live order blocked for {label}: CLOB not available")
            return None

        for attempt in range(1, MAX_ORDER_RETRIES + 1):
            try:
                args = OrderArgs(
                    token_id=token_id,
                    price=round(price, 2),
                    size=round(size, 1),
                    side=BUY,
                )
                signed = self.clob.create_order(args)
                resp = self.clob.post_order(signed, OrderType.GTC)
                oid = resp.get("orderID") if isinstance(resp, dict) else None
                print(f"[ORDER] BUY {label} @ {price:.2f} x {size:.0f}sh | ID: {oid or '?'}")
                return oid
            except Exception as e:
                err = str(e).lower()
                print(f"[!] Order attempt {attempt}: {e}")
                if any(kw in err for kw in ("not enough", "balance", "insufficient")):
                    send_telegram(f"⚠️ Low balance for {label}!")
                    return None
                if attempt < MAX_ORDER_RETRIES:
                    time.sleep(RETRY_DELAY)
        return None

    def submit_sell(
        self, token_id: str, price: float, size: float, label: str
    ) -> str | None:
        if self.clob is None or OrderArgs is None:
            return None

        for attempt in range(1, MAX_ORDER_RETRIES + 1):
            try:
                args = OrderArgs(
                    token_id=token_id,
                    price=round(price, 2),
                    size=round(size, 1),
                    side=SELL,
                )
                signed = self.clob.create_order(args)
                resp = self.clob.post_order(signed, OrderType.GTC)
                oid = resp.get("orderID") if isinstance(resp, dict) else None
                print(f"[SELL] {label} @ {price:.2f} x {size:.0f}sh | ID: {oid or '?'}")
                return oid
            except Exception as e:
                print(f"[!] Sell attempt {attempt}: {e}")
                if attempt < MAX_ORDER_RETRIES:
                    time.sleep(RETRY_DELAY)
        return None

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a resting GTC order."""
        if self.clob is None:
            return False
        try:
            self.clob.cancel(order_id)
            print(f"[CANCEL] {order_id}")
            return True
        except Exception as e:
            print(f"[!] Cancel failed: {e}")
            return False

    def update_balance_allowance(self, token_id: str) -> None:
        if self.clob is None:
            return
        try:
            params = BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL,
                token_id=token_id,
            )
            self.clob.update_balance_allowance(params)
        except Exception:
            pass
