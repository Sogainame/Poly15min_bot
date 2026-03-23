"""15-Minute BTC Straddle Bot — Core Engine.

Strategy:
  1. At start of each 15-min window, buy BOTH UP and DOWN tokens
  2. Set take-profit on BOTH sides
  3. When BTC moves → one side hits TP → sell it
  4. Wait for BTC reversal → other side hits TP → sell it
  5. If only one TP hits, hold the other to expiry (50/50 chance of $1.00)
  6. Worst case: neither TP hits → lose spread only (~$0.04/pair)

Edge: BTC oscillates within 15 min. Both sides can profit from the same move.
"""
from __future__ import annotations

import csv
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from market import PolymarketClient, Book
from notifier import send_telegram

WINDOW_SECS = 900  # 15 minutes
DRY_RUN_BALANCE = 20.0
MIN_BET_USD = 1.0
BALANCE_RESERVE = 1.00
CSV_DIR = Path("data/straddle")

# ── Entry/exit parameters ────────────────────────────────────────────────────
# Entry: buy both sides at market open (first 60 seconds)
ENTRY_WINDOW_SECS = 60       # buy within first 60s of window
MAX_ENTRY_PRICE = 0.55       # don't buy if token already > $0.55
MIN_ENTRY_PRICE = 0.45       # don't buy if token < $0.45 (too skewed)
MAX_ENTRY_SPREAD = 0.06      # skip if book spread > 6 cents
MAX_SUM_PRICE = 1.06         # skip if UP+DOWN ask > $1.06 (too expensive)

# Take-profit: sell when token reaches entry + TP
TAKE_PROFIT = 0.04           # $0.04 per side (sell at ~$0.56)

# Auto-sell winner at expiry
EXPIRY_SELL_PRICE = 0.99     # sell winning tokens at $0.99 after resolution


@dataclass
class LegState:
    """State for one side of the straddle (UP or DOWN)."""
    side: str = ""             # "UP" or "DOWN"
    token_id: str = ""
    entry_price: float = 0.0
    shares: float = 0.0
    order_id: str | None = None
    filled: bool = False
    sold: bool = False
    sell_price: float = 0.0
    sell_order_id: str | None = None
    tp_target: float = 0.0    # entry_price + TAKE_PROFIT


@dataclass
class WindowState:
    """All mutable state for one 15-min trading window."""
    window_ts: int = 0
    slug: str = ""
    condition_id: str = ""
    up_token: str = ""
    down_token: str = ""
    up_leg: LegState = field(default_factory=LegState)
    down_leg: LegState = field(default_factory=LegState)
    entry_attempted: bool = False
    market_found: bool = False
    finalized: bool = False


@dataclass
class Stats:
    windows: int = 0
    entered: int = 0
    skipped: int = 0
    both_tp: int = 0
    single_tp: int = 0
    no_tp: int = 0
    pnl: float = 0.0


class Straddle:
    def __init__(
        self,
        client: PolymarketClient,
        dry_run: bool = True,
        shares: float = 10.0,
        take_profit: float = TAKE_PROFIT,
    ) -> None:
        self.client = client
        self.dry_run = dry_run
        self.shares = shares
        self.take_profit = take_profit
        self.state = WindowState()
        self.stats = Stats()
        self.running = False
        self._last_heartbeat_ts = 0.0
        CSV_DIR.mkdir(parents=True, exist_ok=True)

    # ── Timing ───────────────────────────────────────────────────────────

    def _window_ts(self, now: float | None = None) -> int:
        """Current 15-min window timestamp (aligned to 900s)."""
        ts = time.time() if now is None else now
        return int(ts - (ts % WINDOW_SECS))

    def _secs_into_window(self, now: float | None = None) -> float:
        """Seconds elapsed since window start."""
        ts = time.time() if now is None else now
        return ts - self._window_ts(ts)

    def _secs_left(self, now: float | None = None) -> float:
        ts = time.time() if now is None else now
        return self._window_ts(ts) + WINDOW_SECS - ts

    # ── Market Discovery ─────────────────────────────────────────────────

    def _ensure_market(self) -> bool:
        """Find the 15-min market for current window."""
        if self.state.market_found:
            return True
        market = self.client.find_15m_market(self.state.window_ts)
        if not market:
            return False
        self.state.slug = market["slug"]
        self.state.condition_id = market.get("condition_id", "")
        self.state.up_token = market.get("up_token", "")
        self.state.down_token = market.get("down_token", "")
        if self.state.up_token and self.state.down_token:
            self.state.market_found = True
            return True
        return False

    # ── Entry: Buy Both Sides ────────────────────────────────────────────

    def _try_entry(self) -> bool:
        """Buy UP + DOWN tokens at start of window."""
        s = self.state
        if s.entry_attempted:
            return False

        secs_in = self._secs_into_window()
        if secs_in > ENTRY_WINDOW_SECS:
            s.entry_attempted = True
            print(f"[STRADDLE] ⏰ Entry window closed (T+{secs_in:.0f}s)")
            self.stats.skipped += 1
            return False

        if not self._ensure_market():
            return False

        # Check both books
        up_book = self.client.fetch_book(s.up_token)
        down_book = self.client.fetch_book(s.down_token)

        up_ask = up_book.best_ask
        down_ask = down_book.best_ask

        # Validation
        if up_ask <= 0 or down_ask <= 0:
            print(f"[STRADDLE] ⚠ Empty book: UP ask={up_ask:.3f} DOWN ask={down_ask:.3f}")
            return False

        if up_book.spread > MAX_ENTRY_SPREAD or down_book.spread > MAX_ENTRY_SPREAD:
            print(
                f"[STRADDLE] ⚠ Wide spread: UP={up_book.spread:.3f} DOWN={down_book.spread:.3f}"
            )
            return False

        if up_ask > MAX_ENTRY_PRICE or down_ask > MAX_ENTRY_PRICE:
            print(
                f"[STRADDLE] ⚠ Price too high: UP={up_ask:.3f} DOWN={down_ask:.3f}"
            )
            return False

        if up_ask < MIN_ENTRY_PRICE or down_ask < MIN_ENTRY_PRICE:
            print(
                f"[STRADDLE] ⚠ Price too low (skewed market): UP={up_ask:.3f} DOWN={down_ask:.3f}"
            )
            return False

        sum_price = up_ask + down_ask
        if sum_price > MAX_SUM_PRICE:
            print(f"[STRADDLE] ⚠ Sum too high: ${sum_price:.3f} > ${MAX_SUM_PRICE:.2f}")
            return False

        # Check balance
        total_cost = (up_ask + down_ask) * self.shares
        bal = self.client.get_balance()
        if self.dry_run and (bal is None or bal < total_cost):
            bal = DRY_RUN_BALANCE
        if bal is not None and bal < total_cost + BALANCE_RESERVE:
            print(f"[STRADDLE] ⚠ Low balance: ${bal:.2f} < ${total_cost + BALANCE_RESERVE:.2f}")
            return False

        s.entry_attempted = True

        # Round prices to 2 decimal places
        up_price = round(up_ask, 2)
        down_price = round(down_ask, 2)

        print(
            f"\n[STRADDLE] 🎯 ENTRY: UP@${up_price:.2f} + DOWN@${down_price:.2f}"
            f" = ${up_price + down_price:.2f}/pair x {self.shares:.0f}sh"
            f" = ${total_cost:.2f} total"
        )

        # Place orders
        up_oid: str | None
        down_oid: str | None

        if self.dry_run:
            up_oid = f"DRY-UP-{s.window_ts}"
            down_oid = f"DRY-DOWN-{s.window_ts}"
            print(f"[STRADDLE] [DRY] Would buy UP + DOWN")
        else:
            up_oid = self.client.submit_maker_buy(
                s.up_token, up_price, self.shares, f"STRADDLE-UP-{s.window_ts}"
            )
            down_oid = self.client.submit_maker_buy(
                s.down_token, down_price, self.shares, f"STRADDLE-DOWN-{s.window_ts}"
            )
            if not up_oid or not down_oid:
                print(f"[STRADDLE] ❌ Entry failed — order(s) not placed")
                # Cancel any placed order
                if up_oid:
                    self.client.cancel_order(up_oid)
                if down_oid:
                    self.client.cancel_order(down_oid)
                return False

        # Record leg state
        s.up_leg = LegState(
            side="UP",
            token_id=s.up_token,
            entry_price=up_price,
            shares=self.shares,
            order_id=up_oid,
            filled=True,  # GTC maker — assume fill for now
            tp_target=round(up_price + self.take_profit, 2),
        )
        s.down_leg = LegState(
            side="DOWN",
            token_id=s.down_token,
            entry_price=down_price,
            shares=self.shares,
            order_id=down_oid,
            filled=True,
            tp_target=round(down_price + self.take_profit, 2),
        )

        self.stats.entered += 1

        if not self.dry_run:
            send_telegram(
                f"🎯 STRADDLE ENTRY\n"
                f"UP@${up_price:.2f} + DOWN@${down_price:.2f}\n"
                f"TP targets: ${s.up_leg.tp_target:.2f} / ${s.down_leg.tp_target:.2f}\n"
                f"Cost: ${total_cost:.2f} ({self.shares:.0f}sh each)"
            )

        return True

    # ── Take-Profit Monitoring ───────────────────────────────────────────

    def _check_tp(self, leg: LegState) -> bool:
        """Check if a leg has hit its take-profit target."""
        if not leg.filled or leg.sold or not leg.token_id:
            return False

        book = self.client.fetch_book(leg.token_id)
        bid = book.best_bid
        if bid <= 0:
            return False

        if bid >= leg.tp_target:
            sell_price = min(round(bid, 2), 0.99)
            profit = (sell_price - leg.entry_price) * leg.shares
            print(
                f"\n[STRADDLE] 💰 TP HIT: {leg.side} @ ${sell_price:.2f}"
                f" (entry ${leg.entry_price:.2f}) +${profit:.2f}"
            )

            if self.dry_run:
                leg.sell_order_id = f"DRY-SELL-{leg.side}"
            else:
                oid = self.client.submit_sell(
                    leg.token_id, sell_price, leg.shares,
                    f"STRADDLE-TP-{leg.side}",
                )
                if not oid:
                    print(f"[STRADDLE] ⚠ Sell failed for {leg.side}")
                    return False
                leg.sell_order_id = oid

            leg.sold = True
            leg.sell_price = sell_price

            if not self.dry_run:
                send_telegram(
                    f"💰 TP HIT: {leg.side} @ ${sell_price:.2f}"
                    f" (+${profit:.2f})"
                )
            return True
        return False

    # ── Window Finalization ──────────────────────────────────────────────

    def _finalize_window(self) -> None:
        """Called when window ends. Resolve remaining positions."""
        s = self.state
        if s.finalized or not s.entry_attempted:
            return
        s.finalized = True

        up = s.up_leg
        down = s.down_leg

        # If we never entered, nothing to finalize
        if not up.filled and not down.filled:
            return

        up_sold = up.sold
        down_sold = down.sold

        # Determine resolution
        resolved = self.client.get_market_resolution(s.slug) if s.slug else None

        # Calculate P&L
        up_pnl = 0.0
        down_pnl = 0.0

        if up.filled:
            if up_sold:
                up_pnl = (up.sell_price - up.entry_price) * up.shares
            elif resolved == "UP":
                # UP won — token worth $1.00
                up_pnl = (1.00 - up.entry_price) * up.shares
                self._auto_sell_winner(up)
            else:
                # UP lost — token worth $0.00
                up_pnl = -up.entry_price * up.shares

        if down.filled:
            if down_sold:
                down_pnl = (down.sell_price - down.entry_price) * down.shares
            elif resolved == "DOWN":
                down_pnl = (1.00 - down.entry_price) * down.shares
                self._auto_sell_winner(down)
            else:
                down_pnl = -down.entry_price * down.shares

        total_pnl = round(up_pnl + down_pnl, 2)
        self.stats.pnl += total_pnl

        # Classify outcome
        if up_sold and down_sold:
            outcome = "BOTH_TP"
            self.stats.both_tp += 1
            emoji = "🎉"
        elif up_sold or down_sold:
            outcome = "SINGLE_TP"
            self.stats.single_tp += 1
            emoji = "✅" if total_pnl >= 0 else "⚠️"
        else:
            outcome = "NO_TP"
            self.stats.no_tp += 1
            emoji = "✅" if total_pnl >= 0 else "❌"

        print(
            f"\n[STRADDLE] {emoji} {outcome}: UP={'SOLD' if up_sold else resolved or '?'}"
            f" DOWN={'SOLD' if down_sold else resolved or '?'}"
            f" PnL=${total_pnl:+.2f}"
        )
        print(
            f"[STRADDLE] 📊 Both={self.stats.both_tp} Single={self.stats.single_tp}"
            f" None={self.stats.no_tp} Total PnL=${self.stats.pnl:+.2f}"
        )

        if not self.dry_run:
            bal = self.client.get_balance()
            bal_str = f"${bal:.2f}" if bal else "?"
            send_telegram(
                f"{emoji} {outcome} PnL=${total_pnl:+.2f}\n"
                f"Both={self.stats.both_tp} Single={self.stats.single_tp}"
                f" None={self.stats.no_tp}\n"
                f"Total PnL=${self.stats.pnl:+.2f} Bal={bal_str}"
            )

        self._log_trade(outcome, total_pnl, up_pnl, down_pnl, resolved)

    def _auto_sell_winner(self, leg: LegState) -> None:
        """After resolution, sell winning tokens at $0.99 to recycle USDC."""
        if self.dry_run or leg.sold:
            return
        time.sleep(3)
        self.client.update_balance_allowance(leg.token_id)
        oid = self.client.submit_sell(
            leg.token_id, EXPIRY_SELL_PRICE, leg.shares,
            f"STRADDLE-CLAIM-{leg.side}",
        )
        if oid:
            print(f"[STRADDLE] 💰 Sold {leg.side} winner → USDC recycled")
        else:
            print(f"[STRADDLE] ⚠ Sell {leg.side} winner failed — claim manually")

    # ── CSV Logging ──────────────────────────────────────────────────────

    def _log_trade(
        self, outcome: str, total_pnl: float,
        up_pnl: float, down_pnl: float, resolved: str | None,
    ) -> None:
        s = self.state
        path = CSV_DIR / "trades.csv"
        is_new = not path.exists()
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "slug": s.slug,
            "window_ts": s.window_ts,
            "up_entry": round(s.up_leg.entry_price, 2),
            "down_entry": round(s.down_leg.entry_price, 2),
            "sum_entry": round(s.up_leg.entry_price + s.down_leg.entry_price, 2),
            "shares": round(self.shares, 0),
            "tp": self.take_profit,
            "up_sold": int(s.up_leg.sold),
            "up_sell_price": round(s.up_leg.sell_price, 2) if s.up_leg.sold else "",
            "down_sold": int(s.down_leg.sold),
            "down_sell_price": round(s.down_leg.sell_price, 2) if s.down_leg.sold else "",
            "resolved": resolved or "",
            "outcome": outcome,
            "up_pnl": round(up_pnl, 2),
            "down_pnl": round(down_pnl, 2),
            "total_pnl": round(total_pnl, 2),
            "cum_pnl": round(self.stats.pnl, 2),
        }
        with path.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(row.keys()))
            if is_new:
                writer.writeheader()
            writer.writerow(row)

    # ── Main Loop ────────────────────────────────────────────────────────

    def step(self) -> None:
        """One tick of the bot. Called every ~1 second."""
        now = time.time()
        current_window = self._window_ts(now)

        # First tick — initialize
        if self.state.window_ts == 0:
            self.state = WindowState(window_ts=current_window)
            self.stats.windows += 1

        # New window — finalize previous, reset
        if current_window != self.state.window_ts:
            self._finalize_window()
            self.state = WindowState(window_ts=current_window)
            self.stats.windows += 1

        secs_in = self._secs_into_window(now)
        secs_left = self._secs_left(now)

        # Phase 1: Entry (first 60 seconds)
        s = self.state
        if not s.entry_attempted and secs_in <= ENTRY_WINDOW_SECS:
            self._try_entry()

        # Phase 2: Monitor take-profits
        if s.up_leg.filled and not s.up_leg.sold:
            self._check_tp(s.up_leg)
        if s.down_leg.filled and not s.down_leg.sold:
            self._check_tp(s.down_leg)

        # Heartbeat (every 15s)
        if now - self._last_heartbeat_ts >= 15:
            self._last_heartbeat_ts = now

            t_start = datetime.fromtimestamp(s.window_ts, timezone.utc).strftime("%H:%M")
            t_end = datetime.fromtimestamp(s.window_ts + WINDOW_SECS, timezone.utc).strftime("%H:%M")

            up_status = "SOLD" if s.up_leg.sold else f"${self._current_bid(s.up_leg):.2f}" if s.up_leg.filled else "—"
            down_status = "SOLD" if s.down_leg.sold else f"${self._current_bid(s.down_leg):.2f}" if s.down_leg.filled else "—"

            entered_str = "✅" if s.up_leg.filled else "⏳" if not s.entry_attempted else "⛔"

            print(
                f"[STRADDLE] {t_start}-{t_end} T+{secs_in:.0f}s "
                f"{entered_str} UP={up_status} DOWN={down_status} "
                f"T-{secs_left:.0f}s"
            )

    def _current_bid(self, leg: LegState) -> float:
        """Quick bid check for heartbeat display (cached would be better)."""
        if not leg.token_id:
            return 0.0
        try:
            book = self.client.fetch_book(leg.token_id)
            return book.best_bid
        except Exception:
            return 0.0

    def run(self) -> None:
        mode_label = "LIVE" if not self.dry_run else "DRY"
        bal = self.client.get_balance()
        bal_s = f"${bal:.2f}" if bal else "n/a"

        print(f"\n{'─' * 60}")
        print(f"  📊 BTC 15-Min Straddle Bot — {mode_label}")
        print(f"  Shares: {self.shares:.0f} per side | TP: ${self.take_profit:.2f}")
        print(f"  Entry window: first {ENTRY_WINDOW_SECS}s | Max sum: ${MAX_SUM_PRICE:.2f}")
        print(f"  Entry price: [{MIN_ENTRY_PRICE:.2f}, {MAX_ENTRY_PRICE:.2f}]"
              f" | Max spread: ${MAX_ENTRY_SPREAD:.2f}")
        print(f"  Balance: {bal_s}")
        print(f"{'─' * 60}")

        self.running = True
        while self.running:
            try:
                self.step()
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"[!] Step error: {e}")
            time.sleep(1.0)

    def summary(self) -> str:
        s = self.stats
        total = s.both_tp + s.single_tp + s.no_tp
        return (
            f"Windows={s.windows} Entered={s.entered} "
            f"Both_TP={s.both_tp} Single_TP={s.single_tp} No_TP={s.no_tp} "
            f"PnL=${s.pnl:+.2f}"
        )
