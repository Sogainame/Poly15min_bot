"""Dump-and-Hedge strategy for Polymarket 15m BTC Up/Down.

Core logic (proven by dev-protocol/polymarket-arbitrage-bot):
1. Wait for a DUMP: one side's ask drops ≥ DUMP_THRESHOLD (15%) in LOOKBACK seconds
2. Buy the dumped side (Leg 1) — it's cheap
3. Wait for HEDGE condition: leg1_avg + opposite_ask ≤ SUM_TARGET (0.95)
4. Buy opposite side (Leg 2) — locks in guaranteed profit at resolution
5. If hedge condition not met within MAX_HEDGE_WAIT → forced hedge (stop-loss)
6. Equal shares on both sides. Always.

What this does NOT do (vs old gabagool):
- Does NOT buy at normal volatility
- Does NOT buy sides independently
- Does NOT allow excess shares
- Does NOT skip order fill verification
"""
from __future__ import annotations

import csv
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from market import Book, PolymarketClient
from notifier import send_telegram
from redeemer import redeem_all

# ── Strategy Parameters ──────────────────────────────────────────────
WINDOW_SECS = 900              # 15 minutes
DUMP_THRESHOLD = 0.12          # 12% price drop = dump detected
DUMP_LOOKBACK_SECS = 10        # compare current ask vs ask N seconds ago
DUMP_WINDOW_SECS = 480         # only look for dumps in first 8 min of window
SUM_TARGET = 0.95              # hedge when leg1_avg + opposite_ask ≤ this
MAX_HEDGE_WAIT_SECS = 180      # if no hedge in 3 min after leg1 → forced hedge
SHARES_PER_LEG = 6             # shares to buy per leg
MAX_BUY_PRICE = 0.53           # never pay more than this for any leg
MIN_DUMP_ASK = 0.05            # don't buy if ask is too low (already near resolution)
BALANCE_RESERVE = 1.0          # keep $1 reserve
STOP_BUY_SECS = 600            # no new entries after 10 min into window
POLL_INTERVAL = 2.0            # seconds between each step
ORDER_POLL_TIMEOUT = 15        # seconds to wait for order fill
ORDER_POLL_INTERVAL = 2        # seconds between fill checks

CSV_DIR = Path("data/dump_hedge")


@dataclass
class PriceHistory:
    """Circular buffer of (timestamp, ask) tuples for dump detection."""
    buffer: list[tuple[float, float]] = field(default_factory=list)
    max_age: float = 30.0  # keep last 30s of prices

    def add(self, ts: float, ask: float) -> None:
        self.buffer.append((ts, ask))
        cutoff = ts - self.max_age
        self.buffer = [(t, p) for t, p in self.buffer if t >= cutoff]

    def get_ask_at(self, ts: float, lookback: float) -> float | None:
        """Get the ask price from ~lookback seconds ago."""
        target = ts - lookback
        best = None
        best_diff = float("inf")
        for t, p in self.buffer:
            diff = abs(t - target)
            if diff < best_diff:
                best_diff = diff
                best = p
        if best is not None and best_diff < lookback * 0.5:
            return best
        return None


@dataclass
class WindowState:
    window_ts: int = 0
    slug: str = ""
    up_token: str = ""
    down_token: str = ""
    finalized: bool = False

    # Leg tracking
    leg1_side: str = ""          # "UP" or "DOWN"
    leg1_avg_price: float = 0.0
    leg1_shares: float = 0.0
    leg1_cost: float = 0.0
    leg1_order_id: str = ""
    leg1_filled: bool = False
    leg1_time: float = 0.0       # when leg1 was bought

    leg2_side: str = ""
    leg2_avg_price: float = 0.0
    leg2_shares: float = 0.0
    leg2_cost: float = 0.0
    leg2_order_id: str = ""
    leg2_filled: bool = False

    # Price history for dump detection
    up_history: PriceHistory = field(default_factory=PriceHistory)
    down_history: PriceHistory = field(default_factory=PriceHistory)

    @property
    def has_leg1(self) -> bool:
        return self.leg1_filled and self.leg1_shares > 0

    @property
    def has_leg2(self) -> bool:
        return self.leg2_filled and self.leg2_shares > 0

    @property
    def pair_cost(self) -> float:
        if self.has_leg1 and self.has_leg2:
            return self.leg1_avg_price + self.leg2_avg_price
        return 0.0


@dataclass
class Stats:
    windows: int = 0
    hedged: int = 0          # both legs filled = guaranteed profit
    forced_hedge: int = 0    # stop-loss hedge
    incomplete: int = 0      # only leg1, no hedge possible
    skipped: int = 0         # no dump detected
    pnl: float = 0.0
    buys: int = 0


class DumpHedge:
    def __init__(
        self,
        client: PolymarketClient,
        dry_run: bool = True,
        shares: float = SHARES_PER_LEG,
        sum_target: float = SUM_TARGET,
    ) -> None:
        self.client = client
        self.dry_run = dry_run
        self.shares = shares
        self.sum_target = sum_target
        self.state = WindowState()
        self.stats = Stats()
        self.running = False
        self._last_heartbeat = 0.0
        self._dry_balance = 100.0
        CSV_DIR.mkdir(parents=True, exist_ok=True)

    # ── Timing ───────────────────────────────────────────────────────

    def _window_ts(self, now: float | None = None) -> int:
        t = int(now or time.time())
        return t - (t % WINDOW_SECS)

    def _secs_into_window(self) -> float:
        return time.time() - self.state.window_ts

    def _secs_left(self) -> float:
        return WINDOW_SECS - self._secs_into_window()

    # ── Market Discovery ─────────────────────────────────────────────

    def _discover_market(self) -> bool:
        s = self.state
        wts = self._window_ts()

        if s.window_ts == wts and s.up_token:
            return True

        if s.window_ts != wts and s.window_ts > 0 and not s.finalized:
            self._finalize_window()

        # New window
        s.window_ts = wts
        s.slug = ""
        s.up_token = ""
        s.down_token = ""
        s.finalized = False
        s.leg1_side = ""
        s.leg1_avg_price = 0.0
        s.leg1_shares = 0.0
        s.leg1_cost = 0.0
        s.leg1_order_id = ""
        s.leg1_filled = False
        s.leg1_time = 0.0
        s.leg2_side = ""
        s.leg2_avg_price = 0.0
        s.leg2_shares = 0.0
        s.leg2_cost = 0.0
        s.leg2_order_id = ""
        s.leg2_filled = False
        s.up_history = PriceHistory()
        s.down_history = PriceHistory()

        self.stats.windows += 1

        mkt = self.client.find_15m_market(wts)
        if not mkt:
            return False

        s.slug = mkt["slug"]
        s.up_token = mkt["up_token"]
        s.down_token = mkt["down_token"]
        return bool(s.up_token and s.down_token)

    # ── Dump Detection ───────────────────────────────────────────────

    def _detect_dump(self, up_ask: float, down_ask: float) -> str | None:
        """Return 'UP' or 'DOWN' if a dump is detected on that side, else None."""
        now = time.time()
        s = self.state
        s.up_history.add(now, up_ask)
        s.down_history.add(now, down_ask)

        # Only detect dumps in the first DUMP_WINDOW_SECS
        if self._secs_into_window() > DUMP_WINDOW_SECS:
            return None

        # Check UP dump: UP ask dropped significantly
        up_prev = s.up_history.get_ask_at(now, DUMP_LOOKBACK_SECS)
        if up_prev and up_prev > 0.10:
            up_drop = (up_prev - up_ask) / up_prev
            if up_drop >= DUMP_THRESHOLD and up_ask >= MIN_DUMP_ASK and up_ask <= MAX_BUY_PRICE:
                return "UP"

        # Check DOWN dump: DOWN ask dropped significantly
        dn_prev = s.down_history.get_ask_at(now, DUMP_LOOKBACK_SECS)
        if dn_prev and dn_prev > 0.10:
            dn_drop = (dn_prev - down_ask) / dn_prev
            if dn_drop >= DUMP_THRESHOLD and down_ask >= MIN_DUMP_ASK and down_ask <= MAX_BUY_PRICE:
                return "DOWN"

        return None

    # ── Order Execution ──────────────────────────────────────────────

    def _buy(self, side: str, price: float, label: str) -> tuple[str, bool]:
        """Place buy order. Returns (order_id, filled)."""
        buy_price = min(round(price + 0.01, 2), MAX_BUY_PRICE)

        if self.dry_run:
            oid = f"DRY-{label}"
            print(f"[DH] [DRY] BUY {side} @ ${buy_price:.2f} x {self.shares:.0f}sh")
            self._dry_balance -= buy_price * self.shares
            return oid, True  # DRY always fills

        oid = self.client.submit_maker_buy(
            self.state.up_token if side == "UP" else self.state.down_token,
            buy_price, self.shares, label
        )
        if not oid:
            return "", False

        # Poll for fill
        filled = self._poll_order_fill(oid)
        return oid, filled

    def _poll_order_fill(self, order_id: str) -> bool:
        """Poll order status until MATCHED or timeout."""
        deadline = time.time() + ORDER_POLL_TIMEOUT
        while time.time() < deadline:
            status = self.client.get_order_status(order_id)
            if status in ("MATCHED", "FILLED"):
                return True
            if status in ("CANCELLED", "EXPIRED"):
                return False
            time.sleep(ORDER_POLL_INTERVAL)
        # Timeout — check one last time
        status = self.client.get_order_status(order_id)
        return status in ("MATCHED", "FILLED")

    # ── Strategy Step ────────────────────────────────────────────────

    def step(self) -> None:
        if not self._discover_market():
            return

        s = self.state
        secs_in = self._secs_into_window()

        # Fetch prices
        up_book = self.client.fetch_book(s.up_token)
        down_book = self.client.fetch_book(s.down_token)
        up_ask = up_book.best_ask
        down_ask = down_book.best_ask

        if up_ask <= 0 or down_ask <= 0:
            return

        # ── Phase 1: Looking for dump (no position yet) ──────────
        if not s.has_leg1 and not s.leg1_order_id:
            dump_side = self._detect_dump(up_ask, down_ask)
            if dump_side and secs_in <= STOP_BUY_SECS:
                # Check balance
                dump_ask = up_ask if dump_side == "UP" else down_ask
                cost = (dump_ask + 0.01) * self.shares
                bal = self._dry_balance if self.dry_run else (self.client.get_balance() or 0)
                if bal >= cost + BALANCE_RESERVE:
                    label = f"DH-{dump_side}-{s.window_ts}-L1"
                    oid, filled = self._buy(dump_side, dump_ask, label)
                    if oid:
                        buy_price = min(round(dump_ask + 0.01, 2), MAX_BUY_PRICE)
                        s.leg1_side = dump_side
                        s.leg1_order_id = oid
                        s.leg1_filled = filled
                        s.leg1_avg_price = buy_price
                        s.leg1_shares = self.shares
                        s.leg1_cost = buy_price * self.shares
                        s.leg1_time = time.time()
                        self.stats.buys += 1

                        bal_str = f" | bal=${self._dry_balance:.2f}" if self.dry_run else ""
                        print(
                            f"[DH] 🎯 DUMP {dump_side} detected! Bought Leg1 @ ${buy_price:.2f}"
                            f" x {self.shares:.0f}sh{bal_str}"
                        )

        # ── Phase 2: Have leg1, looking for hedge ────────────────
        elif s.has_leg1 and not s.has_leg2 and not s.leg2_order_id:
            opposite = "DOWN" if s.leg1_side == "UP" else "UP"
            opposite_ask = down_ask if s.leg1_side == "UP" else up_ask
            combined = s.leg1_avg_price + opposite_ask

            # Check if hedge condition met
            hedge_ready = combined <= self.sum_target and opposite_ask <= MAX_BUY_PRICE
            forced = False

            # Stop-loss: force hedge if waiting too long
            if not hedge_ready and (time.time() - s.leg1_time) > MAX_HEDGE_WAIT_SECS:
                if opposite_ask <= MAX_BUY_PRICE and secs_in <= STOP_BUY_SECS:
                    hedge_ready = True
                    forced = True

            if hedge_ready:
                cost = (opposite_ask + 0.01) * self.shares
                bal = self._dry_balance if self.dry_run else (self.client.get_balance() or 0)
                if bal >= cost + BALANCE_RESERVE:
                    label = f"DH-{opposite}-{s.window_ts}-L2"
                    oid, filled = self._buy(opposite, opposite_ask, label)
                    if oid:
                        buy_price = min(round(opposite_ask + 0.01, 2), MAX_BUY_PRICE)
                        s.leg2_side = opposite
                        s.leg2_order_id = oid
                        s.leg2_filled = filled
                        s.leg2_avg_price = buy_price
                        s.leg2_shares = self.shares
                        s.leg2_cost = buy_price * self.shares
                        self.stats.buys += 1

                        tag = "⚠️ FORCED" if forced else "✅ HEDGE"
                        bal_str = f" | bal=${self._dry_balance:.2f}" if self.dry_run else ""
                        print(
                            f"[DH] {tag} Leg2 {opposite} @ ${buy_price:.2f}"
                            f" | pair=${s.pair_cost:.3f}"
                            f" | profit=${(1.0 - s.pair_cost) * self.shares:+.2f}"
                            f"{bal_str}"
                        )
                        if forced:
                            self.stats.forced_hedge += 1

        # ── Heartbeat ────────────────────────────────────────────
        now = time.time()
        if now - self._last_heartbeat >= 15:
            self._last_heartbeat = now
            t_start = datetime.fromtimestamp(s.window_ts, timezone.utc).strftime("%H:%M")
            t_end = datetime.fromtimestamp(s.window_ts + WINDOW_SECS, timezone.utc).strftime("%H:%M")

            if s.has_leg1 and s.has_leg2:
                status = f"🎉 HEDGED pair=${s.pair_cost:.3f}"
            elif s.has_leg1:
                opp_ask = down_ask if s.leg1_side == "UP" else up_ask
                combined = s.leg1_avg_price + opp_ask
                wait = int(time.time() - s.leg1_time)
                status = f"⏳ L1={s.leg1_side}@${s.leg1_avg_price:.2f} sum=${combined:.2f} wait={wait}s"
            else:
                status = "👀 scanning"

            print(
                f"[DH] {t_start}-{t_end} T+{secs_in:.0f}s {status}"
                f" UP={up_ask:.2f} DN={down_ask:.2f}"
                f" T-{self._secs_left():.0f}s"
            )

    # ── Window Finalize ──────────────────────────────────────────

    def _finalize_window(self) -> None:
        s = self.state
        if s.finalized:
            return
        s.finalized = True

        if not s.has_leg1:
            self.stats.skipped += 1
            return

        resolved = self.client.get_market_resolution(s.slug) if s.slug else None

        if s.has_leg1 and s.has_leg2:
            # HEDGED — guaranteed profit from equal shares
            self.stats.hedged += 1
            profit = (1.0 - s.pair_cost) * self.shares
            total_pnl = round(profit, 2)
            emoji = "🎉"
            outcome = "HEDGED"
        else:
            # INCOMPLETE — only leg1, pure directional risk
            self.stats.incomplete += 1
            if resolved == s.leg1_side:
                total_pnl = round((1.0 - s.leg1_avg_price) * s.leg1_shares, 2)
            elif resolved:
                total_pnl = round(-s.leg1_cost, 2)
            else:
                # DRY unknown resolution — conservative: return cost (50/50)
                total_pnl = 0.0
            emoji = "✅" if total_pnl >= 0 else "❌"
            outcome = "INCOMPLETE"

        self.stats.pnl += total_pnl

        # DRY balance
        if self.dry_run:
            if s.has_leg1 and s.has_leg2:
                self._dry_balance += self.shares * 1.0  # resolution pays $1 per paired share
            elif s.has_leg1:
                self._dry_balance += s.leg1_cost  # conservative: return cost

        t_start = datetime.fromtimestamp(s.window_ts, timezone.utc).strftime("%H:%M")
        bal_str = f" | bal=${self._dry_balance:.2f}" if self.dry_run else ""
        print(
            f"\n[DH] {emoji} {outcome} @ {t_start}"
            f" | L1={s.leg1_side}@${s.leg1_avg_price:.3f}"
            f" | L2={s.leg2_side or '—'}@${s.leg2_avg_price:.3f}"
            f" | pair=${s.pair_cost:.3f}"
            f" | resolved={resolved or '?'}"
            f" | PnL=${total_pnl:+.2f}{bal_str}"
        )
        print(
            f"[DH] 📊 Hedged={self.stats.hedged}"
            f" Forced={self.stats.forced_hedge}"
            f" Incomplete={self.stats.incomplete}"
            f" Skipped={self.stats.skipped}"
            f" Total PnL=${self.stats.pnl:+.2f}"
        )

        if not self.dry_run:
            bal = self.client.get_balance()
            bal_s = f"${bal:.2f}" if bal else "?"
            send_telegram(
                f"{emoji} {outcome} PnL=${total_pnl:+.2f}\n"
                f"L1={s.leg1_side}@${s.leg1_avg_price:.3f}\n"
                f"L2={s.leg2_side or '—'}@${s.leg2_avg_price:.3f}\n"
                f"pair=${s.pair_cost:.3f} resolved={resolved}\n"
                f"Total=${self.stats.pnl:+.2f} Bal={bal_s}"
            )
            redeem_all()

        self._log_trade(outcome, total_pnl, resolved)

    # ── CSV Logging ──────────────────────────────────────────────

    def _log_trade(self, outcome: str, pnl: float, resolved: str | None) -> None:
        s = self.state
        csv_path = CSV_DIR / "trades.csv"
        write_header = not csv_path.exists()
        try:
            with open(csv_path, "a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=[
                    "ts", "slug", "window_ts",
                    "leg1_side", "leg1_price", "leg1_shares", "leg1_cost",
                    "leg2_side", "leg2_price", "leg2_shares", "leg2_cost",
                    "pair_cost", "resolved", "outcome", "pnl", "cum_pnl",
                ])
                if write_header:
                    w.writeheader()
                w.writerow({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "slug": s.slug,
                    "window_ts": s.window_ts,
                    "leg1_side": s.leg1_side,
                    "leg1_price": round(s.leg1_avg_price, 3),
                    "leg1_shares": s.leg1_shares,
                    "leg1_cost": round(s.leg1_cost, 2),
                    "leg2_side": s.leg2_side,
                    "leg2_price": round(s.leg2_avg_price, 3),
                    "leg2_shares": s.leg2_shares,
                    "leg2_cost": round(s.leg2_cost, 2),
                    "pair_cost": round(s.pair_cost, 3),
                    "resolved": resolved or "",
                    "outcome": outcome,
                    "pnl": round(pnl, 2),
                    "cum_pnl": round(self.stats.pnl, 2),
                })
        except Exception as e:
            print(f"[!] CSV write error: {e}")

    # ── Summary ──────────────────────────────────────────────────

    def summary(self) -> str:
        s = self.stats
        return (
            f"Windows={s.windows} Hedged={s.hedged} Forced={s.forced_hedge}"
            f" Incomplete={s.incomplete} Skipped={s.skipped}"
            f" Buys={s.buys} PnL=${s.pnl:+.2f}"
        )

    # ── Run Loop ─────────────────────────────────────────────────

    def run(self) -> None:
        mode = "LIVE" if not self.dry_run else "DRY"
        if self.dry_run:
            bal_s = f"${self._dry_balance:.2f} (virtual)"
        else:
            bal = self.client.get_balance()
            bal_s = f"${bal:.2f}" if bal else "n/a"

        print(f"\n{'─' * 60}")
        print(f"  💀 Dump & Hedge — {mode}")
        print(f"  Dump threshold: {DUMP_THRESHOLD*100:.0f}% drop in {DUMP_LOOKBACK_SECS}s")
        print(f"  Dump window: first {DUMP_WINDOW_SECS}s | Sum target: ${self.sum_target:.2f}")
        print(f"  Shares/leg: {self.shares:.0f} | Max price: ${MAX_BUY_PRICE:.2f}")
        print(f"  Hedge timeout: {MAX_HEDGE_WAIT_SECS}s | Balance: {bal_s}")
        print(f"{'─' * 60}")

        self.running = True
        while self.running:
            try:
                self.step()
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"[!] Step error: {e}")
            time.sleep(POLL_INTERVAL)
