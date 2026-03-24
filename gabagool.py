"""Gabagool-style spread capture for 15-min BTC Up/Down markets.

Strategy:
  Monitor UP and DOWN ask prices throughout the 15-min window.
  When one side drops below a threshold → buy it (Leg 1).
  When the other side also drops → buy it (Leg 2).
  Track pair_cost = avg_up_price + avg_down_price.
  If pair_cost < $1.00 → guaranteed profit at resolution.
  DCA: if a side keeps dropping, buy more to lower average.

  Key rule: NEVER buy if it would push pair_cost ≥ $0.99.

  Edge: BTC oscillates within 15 min, causing temporary mispricings.
  Gabagool earned $58/window with $1200 capital. We scale to $9.

Reference: coinsbench.com/inside-the-mind-of-a-polymarket-bot
"""
from __future__ import annotations

import csv
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from market import PolymarketClient, Book
from notifier import send_telegram
from redeemer import redeem_all

WINDOW_SECS = 900  # 15 minutes
CSV_DIR = Path("data/gabagool")

# ── Strategy Parameters ─────────────────────────────────────────────────────
BUY_THRESHOLD = 0.47       # buy a side when ask drops to this or below
MAX_PAIR_COST = 0.97       # never let combined avg cost exceed this
MAX_SINGLE_PRICE = 0.53    # never buy a side above this price
MIN_SHARES = 6             # minimum per buy (5 after fee = sellable)
STOP_BUY_SECS = 600        # stop buying after 10 min into window (5 min buffer)
MAX_BUYS_PER_SIDE = 2      # max DCA entries per side per window (was 3)
BALANCE_RESERVE = 1.00     # keep $1 in reserve
DCA_COOLDOWN_SECS = 60     # min seconds between buys on same side
MAX_SINGLE_SIDE_PCT = 0.50 # never spend more than 50% of balance on one side
MAX_OPPOSITE_ASK = 0.55    # don't buy if other side ask > this (no chance of pair)


@dataclass
class LegPosition:
    """Accumulated position on one side (UP or DOWN)."""
    side: str = ""
    token_id: str = ""
    total_shares: float = 0.0
    total_cost: float = 0.0    # total USD spent
    buy_count: int = 0
    order_ids: list[str] = field(default_factory=list)
    last_buy_ts: float = 0.0   # timestamp of last buy (for DCA cooldown)

    @property
    def avg_price(self) -> float:
        if self.total_shares <= 0:
            return 0.0
        return self.total_cost / self.total_shares

    @property
    def has_position(self) -> bool:
        return self.total_shares > 0


@dataclass
class WindowState:
    window_ts: int = 0
    slug: str = ""
    condition_id: str = ""
    up_token: str = ""
    down_token: str = ""
    up_leg: LegPosition = field(default_factory=LegPosition)
    down_leg: LegPosition = field(default_factory=LegPosition)
    market_found: bool = False
    finalized: bool = False

    @property
    def pair_cost(self) -> float:
        """Combined average cost per pair. Must stay < $1.00 for profit."""
        up_avg = self.up_leg.avg_price if self.up_leg.has_position else 0.0
        down_avg = self.down_leg.avg_price if self.down_leg.has_position else 0.0
        if up_avg > 0 and down_avg > 0:
            return up_avg + down_avg
        return 0.0  # incomplete pair, can't calculate yet

    @property
    def both_legs(self) -> bool:
        return self.up_leg.has_position and self.down_leg.has_position

    @property
    def guaranteed_profit(self) -> float:
        """Profit per pair if both legs filled. One side pays $1."""
        if not self.both_legs:
            return 0.0
        min_shares = min(self.up_leg.total_shares, self.down_leg.total_shares)
        return (1.0 - self.pair_cost) * min_shares


@dataclass
class Stats:
    windows: int = 0
    entered: int = 0       # windows where at least one leg bought
    complete: int = 0      # windows where both legs filled (guaranteed profit)
    incomplete: int = 0    # windows where only one leg filled (directional risk)
    skipped: int = 0
    pnl: float = 0.0
    buys: int = 0          # total buy orders across all windows


class Gabagool:
    def __init__(
        self,
        client: PolymarketClient,
        dry_run: bool = True,
        shares: float = 6.0,
        buy_threshold: float = BUY_THRESHOLD,
        max_pair_cost: float = MAX_PAIR_COST,
    ) -> None:
        self.client = client
        self.dry_run = dry_run
        self.shares = shares
        self.buy_threshold = buy_threshold
        self.max_pair_cost = max_pair_cost
        self.state = WindowState()
        self.stats = Stats()
        self.running = False
        self._last_heartbeat_ts = 0.0
        self._dry_balance = 100.0  # virtual balance for DRY simulation
        CSV_DIR.mkdir(parents=True, exist_ok=True)

    # ── Timing ───────────────────────────────────────────────────────────

    def _window_ts(self, now: float | None = None) -> int:
        ts = time.time() if now is None else now
        return int(ts - (ts % WINDOW_SECS))

    def _secs_into_window(self, now: float | None = None) -> float:
        ts = time.time() if now is None else now
        return ts - self._window_ts(ts)

    def _secs_left(self, now: float | None = None) -> float:
        ts = time.time() if now is None else now
        return self._window_ts(ts) + WINDOW_SECS - ts

    # ── Market Discovery ─────────────────────────────────────────────────

    def _ensure_market(self) -> bool:
        if self.state.market_found:
            return True
        market = self.client.find_15m_market(self.state.window_ts)
        if not market:
            return False
        s = self.state
        s.slug = market["slug"]
        s.condition_id = market.get("condition_id", "")
        s.up_token = market.get("up_token", "")
        s.down_token = market.get("down_token", "")
        if s.up_token and s.down_token:
            s.market_found = True
            s.up_leg.side = "UP"
            s.up_leg.token_id = s.up_token
            s.down_leg.side = "DOWN"
            s.down_leg.token_id = s.down_token
            return True
        return False

    # ── Core: Should We Buy This Side? ───────────────────────────────────

    def _should_buy(self, leg: LegPosition, ask: float,
                    other_leg: LegPosition, other_ask: float) -> bool:
        """Decide whether to buy more shares on this side.

        Three new safety rules (from first live test failure):
        1. DCA cooldown: min 60s between buys on same side
        2. Balance reserve: never spend >50% of balance on one side
        3. Opposite ask check: don't buy if other side is too expensive to pair
        """
        # Price too high or zero
        if ask > MAX_SINGLE_PRICE or ask <= 0:
            return False

        # Already maxed DCA on this side
        if leg.buy_count >= MAX_BUYS_PER_SIDE:
            return False

        # Too late in window
        if self._secs_into_window() > STOP_BUY_SECS:
            return False

        # Price at or below threshold — this is a buying opportunity
        if ask > self.buy_threshold:
            return False

        # FIX 1: DCA cooldown — no spam buying
        if leg.last_buy_ts > 0 and (time.time() - leg.last_buy_ts) < DCA_COOLDOWN_SECS:
            return False

        # FIX 4: Equal legs only — no excess shares
        # First buy on each side: always allowed (buy_count=0)
        # DCA (buy_count>0): only if other side has SAME number of buys
        # This ensures legs stay 1:1 or 2:2. Never 2:1 or 2:0.
        if leg.buy_count > 0 and leg.buy_count != other_leg.buy_count:
            return False

        # FIX 3: Don't buy if other side is too expensive to ever form a pair
        # If other side ask > $0.55, pair_cost will be > $0.47+$0.55 = $1.02 — never profitable
        if not other_leg.has_position and other_ask > MAX_OPPOSITE_ASK:
            return False

        # Check balance
        cost = ask * self.shares
        bal = self.client.get_balance()
        if self.dry_run:
            bal = self._dry_balance
        if bal is not None and bal < cost + BALANCE_RESERVE:
            return False

        # FIX 2: Never spend more than 50% of total balance on one side
        # Reserve the rest for the second leg
        if bal is not None and not other_leg.has_position:
            max_on_one_side = bal * MAX_SINGLE_SIDE_PCT
            if leg.total_cost + cost > max_on_one_side:
                return False

        # KEY RULE: would this purchase push pair_cost above max?
        if other_leg.has_position:
            new_shares = leg.total_shares + self.shares
            new_cost = leg.total_cost + (ask * self.shares)
            new_avg = new_cost / new_shares
            projected_pair_cost = new_avg + other_leg.avg_price
            if projected_pair_cost >= self.max_pair_cost:
                return False

        return True

    # ── Execute Buy ──────────────────────────────────────────────────────

    def _buy_leg(self, leg: LegPosition, ask: float) -> bool:
        """Buy shares on one side."""
        buy_price = min(round(ask + 0.01, 2), MAX_SINGLE_PRICE)  # ask+$0.01 for fill

        if self.dry_run:
            oid = f"DRY-{leg.side}-{self.state.window_ts}-{leg.buy_count}"
            print(f"[GABAGOOL] [DRY] BUY {leg.side} @ ${buy_price:.2f} x {self.shares:.0f}sh")
        else:
            oid = self.client.submit_maker_buy(
                leg.token_id, buy_price, self.shares,
                f"GAB-{leg.side}-{self.state.window_ts}-{leg.buy_count}"
            )
            if not oid:
                return False

        leg.total_shares += self.shares
        leg.total_cost += buy_price * self.shares
        leg.buy_count += 1
        leg.last_buy_ts = time.time()  # for DCA cooldown
        leg.order_ids.append(oid or "")
        self.stats.buys += 1

        # Deduct from virtual balance in DRY mode
        if self.dry_run:
            self._dry_balance -= buy_price * self.shares

        s = self.state
        pair_info = ""
        if s.up_leg.has_position and s.down_leg.has_position:
            pair_info = f" | pair_cost=${s.pair_cost:.3f} profit=${s.guaranteed_profit:+.2f}"

        bal_str = f" | bal=${self._dry_balance:.2f}" if self.dry_run else ""
        print(
            f"[GABAGOOL] ✅ BUY {leg.side} #{leg.buy_count} @ ${buy_price:.2f}"
            f" x {self.shares:.0f}sh"
            f" | avg=${leg.avg_price:.3f} total=${leg.total_cost:.2f}"
            f"{pair_info}{bal_str}"
        )

        if not self.dry_run:
            send_telegram(
                f"✅ BUY {leg.side} @ ${buy_price:.2f} x {self.shares:.0f}sh"
                f"\navg=${leg.avg_price:.3f}{pair_info}"
            )

        return True

    # ── Scan & Buy ───────────────────────────────────────────────────────

    def _scan_opportunities(self) -> None:
        """Check both sides for buying opportunities."""
        s = self.state
        if not s.market_found:
            return

        up_book = self.client.fetch_book(s.up_token)
        down_book = self.client.fetch_book(s.down_token)

        up_ask = up_book.best_ask
        down_ask = down_book.best_ask

        # Try to buy cheaper side first (better deal)
        if up_ask <= down_ask:
            sides = [(s.up_leg, up_ask, s.down_leg, down_ask),
                     (s.down_leg, down_ask, s.up_leg, up_ask)]
        else:
            sides = [(s.down_leg, down_ask, s.up_leg, up_ask),
                     (s.up_leg, up_ask, s.down_leg, down_ask)]

        for leg, ask, other, other_ask in sides:
            if self._should_buy(leg, ask, other, other_ask):
                self._buy_leg(leg, ask)

    # ── Window Finalization ──────────────────────────────────────────────

    def _finalize_window(self) -> None:
        s = self.state
        if s.finalized:
            return
        s.finalized = True

        up = s.up_leg
        down = s.down_leg

        # No position at all
        if not up.has_position and not down.has_position:
            self.stats.skipped += 1
            return

        self.stats.entered += 1

        # Get resolution
        resolved = self.client.get_market_resolution(s.slug) if s.slug else None

        # Calculate PnL
        if up.has_position and down.has_position:
            # COMPLETE PAIR — guaranteed profit
            self.stats.complete += 1
            min_shares = min(up.total_shares, down.total_shares)
            profit = (1.0 - s.pair_cost) * min_shares

            # Excess shares on one side (if unbalanced)
            if up.total_shares > down.total_shares:
                excess = up.total_shares - down.total_shares
                if resolved == "UP":
                    profit += (1.0 - up.avg_price) * excess
                elif resolved:  # resolved is known and it's DOWN
                    profit -= up.avg_price * excess
                # else: resolved unknown (DRY) — don't penalize, excess = 50/50 coin flip
            elif down.total_shares > up.total_shares:
                excess = down.total_shares - up.total_shares
                if resolved == "DOWN":
                    profit += (1.0 - down.avg_price) * excess
                elif resolved:  # resolved is known and it's UP
                    profit -= down.avg_price * excess
                # else: resolved unknown (DRY) — don't penalize

            total_pnl = round(profit, 2)
            emoji = "🎉" if total_pnl > 0 else "⚠️"
            outcome = "COMPLETE"

        else:
            # INCOMPLETE — only one leg, directional risk
            self.stats.incomplete += 1
            if up.has_position:
                if resolved == "UP":
                    total_pnl = round((1.0 - up.avg_price) * up.total_shares, 2)
                else:
                    total_pnl = round(-up.total_cost, 2)
            else:
                if resolved == "DOWN":
                    total_pnl = round((1.0 - down.avg_price) * down.total_shares, 2)
                else:
                    total_pnl = round(-down.total_cost, 2)
            emoji = "✅" if total_pnl > 0 else "❌"
            outcome = "INCOMPLETE"

        self.stats.pnl += total_pnl

        # DRY mode: simulate resolution payout
        # Complete pair: min_shares resolve to $1.00 each, excess depends on resolution
        # Incomplete: winning side gets $1.00 per share, losing side gets $0
        if self.dry_run:
            if up.has_position and down.has_position:
                min_sh = min(up.total_shares, down.total_shares)
                self._dry_balance += min_sh * 1.0  # paired shares always pay $1
                # Excess: in DRY we don't know resolution, assume 50/50 → return avg_price
                # (conservative: you get your money back on average)
                if up.total_shares > down.total_shares:
                    self._dry_balance += (up.total_shares - min_sh) * up.avg_price
                elif down.total_shares > up.total_shares:
                    self._dry_balance += (down.total_shares - min_sh) * down.avg_price
            elif up.has_position:
                # 50/50 → return avg (conservative)
                self._dry_balance += up.total_cost
            elif down.has_position:
                self._dry_balance += down.total_cost

        t_start = datetime.fromtimestamp(s.window_ts, timezone.utc).strftime("%H:%M")
        bal_str = f" | bal=${self._dry_balance:.2f}" if self.dry_run else ""
        print(
            f"\n[GABAGOOL] {emoji} {outcome} @ {t_start}"
            f" | UP: {up.buy_count}buys avg=${up.avg_price:.3f}"
            f" | DOWN: {down.buy_count}buys avg=${down.avg_price:.3f}"
            f" | pair=${s.pair_cost:.3f}"
            f" | resolved={resolved or '?'}"
            f" | PnL=${total_pnl:+.2f}{bal_str}"
        )
        print(
            f"[GABAGOOL] 📊 Complete={self.stats.complete}"
            f" Incomplete={self.stats.incomplete}"
            f" Skipped={self.stats.skipped}"
            f" Buys={self.stats.buys}"
            f" Total PnL=${self.stats.pnl:+.2f}"
        )

        if not self.dry_run:
            bal = self.client.get_balance()
            bal_str = f"${bal:.2f}" if bal else "?"
            send_telegram(
                f"{emoji} {outcome} PnL=${total_pnl:+.2f}\n"
                f"UP: {up.buy_count}x avg=${up.avg_price:.3f}\n"
                f"DOWN: {down.buy_count}x avg=${down.avg_price:.3f}\n"
                f"pair=${s.pair_cost:.3f} resolved={resolved}\n"
                f"Total=${self.stats.pnl:+.2f} Bal={bal_str}"
            )
            # Redeem resolved positions
            redeem_all()

        self._log_trade(outcome, total_pnl, resolved)

    # ── CSV Logging ──────────────────────────────────────────────────────

    def _log_trade(self, outcome: str, total_pnl: float, resolved: str | None) -> None:
        s = self.state
        path = CSV_DIR / "trades.csv"
        is_new = not path.exists()
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "slug": s.slug,
            "window_ts": s.window_ts,
            "up_buys": s.up_leg.buy_count,
            "up_shares": round(s.up_leg.total_shares, 1),
            "up_avg": round(s.up_leg.avg_price, 3),
            "up_cost": round(s.up_leg.total_cost, 2),
            "down_buys": s.down_leg.buy_count,
            "down_shares": round(s.down_leg.total_shares, 1),
            "down_avg": round(s.down_leg.avg_price, 3),
            "down_cost": round(s.down_leg.total_cost, 2),
            "pair_cost": round(s.pair_cost, 3),
            "resolved": resolved or "",
            "outcome": outcome,
            "pnl": round(total_pnl, 2),
            "cum_pnl": round(self.stats.pnl, 2),
        }
        with path.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(row.keys()))
            if is_new:
                writer.writeheader()
            writer.writerow(row)

    # ── Main Loop ────────────────────────────────────────────────────────

    def step(self) -> None:
        now = time.time()
        current_window = self._window_ts(now)

        # First tick
        if self.state.window_ts == 0:
            self.state = WindowState(window_ts=current_window)
            self.stats.windows += 1

        # New window
        if current_window != self.state.window_ts:
            self._finalize_window()
            self.state = WindowState(window_ts=current_window)
            self.stats.windows += 1

        # Ensure market found
        if not self._ensure_market():
            return

        secs_in = self._secs_into_window(now)
        secs_left = self._secs_left(now)
        s = self.state

        # Scan for buying opportunities (first 10 min of window)
        if secs_in <= STOP_BUY_SECS:
            self._scan_opportunities()

        # Heartbeat every 15s
        if now - self._last_heartbeat_ts >= 15:
            self._last_heartbeat_ts = now

            t_start = datetime.fromtimestamp(s.window_ts, timezone.utc).strftime("%H:%M")
            t_end = datetime.fromtimestamp(s.window_ts + WINDOW_SECS, timezone.utc).strftime("%H:%M")

            up_str = f"${s.up_leg.avg_price:.2f}x{s.up_leg.buy_count}" if s.up_leg.has_position else "—"
            down_str = f"${s.down_leg.avg_price:.2f}x{s.down_leg.buy_count}" if s.down_leg.has_position else "—"

            # Show current asks for monitoring
            up_book = self.client.fetch_book(s.up_token) if s.up_token else Book()
            down_book = self.client.fetch_book(s.down_token) if s.down_token else Book()

            status = "🎉 PAIRED" if s.both_legs else "⏳" if (s.up_leg.has_position or s.down_leg.has_position) else "👀"
            pair_str = f" pair=${s.pair_cost:.3f}" if s.both_legs else ""

            print(
                f"[GABAGOOL] {t_start}-{t_end} T+{secs_in:.0f}s {status}"
                f" UP={up_str}(ask={up_book.best_ask:.2f})"
                f" DN={down_str}(ask={down_book.best_ask:.2f})"
                f"{pair_str} T-{secs_left:.0f}s"
            )

    def run(self) -> None:
        mode_label = "LIVE" if not self.dry_run else "DRY"
        bal = self.client.get_balance()
        if self.dry_run:
            bal_s = f"${self._dry_balance:.2f} (virtual)"
        else:
            bal_s = f"${bal:.2f}" if bal else "n/a"

        print(f"\n{'─' * 60}")
        print(f"  🍝 Gabagool Spread Capture — {mode_label}")
        print(f"  Buy when ask ≤ ${self.buy_threshold:.2f} | Max pair cost: ${self.max_pair_cost:.2f}")
        print(f"  Shares per buy: {self.shares:.0f} | Max buys/side: {MAX_BUYS_PER_SIDE}")
        print(f"  DCA cooldown: {DCA_COOLDOWN_SECS}s | Max opposite ask: ${MAX_OPPOSITE_ASK:.2f}")
        print(f"  Max single side: {MAX_SINGLE_SIDE_PCT*100:.0f}% of balance")
        print(f"  Stop buying after: {STOP_BUY_SECS}s | Balance: {bal_s}")
        print(f"{'─' * 60}")

        self.running = True
        while self.running:
            try:
                self.step()
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"[!] Step error: {e}")
            time.sleep(2.0)  # poll every 2s (not 1s — less API spam)

    def summary(self) -> str:
        s = self.stats
        return (
            f"Windows={s.windows} Complete={s.complete} Incomplete={s.incomplete}"
            f" Skipped={s.skipped} Buys={s.buys} PnL=${s.pnl:+.2f}"
        )
