"""BTC 15-Minute Bot — Entry Point.

Strategies:
  straddle  — buy both sides at open, TP on oscillation (original)
  gabagool  — asymmetric spread capture, buy each side when cheap (new)

Usage:
    python bot.py                              # DRY gabagool (default)
    python bot.py --live                       # LIVE gabagool
    python bot.py --strategy straddle --live   # LIVE straddle
    python bot.py --live --threshold 0.47      # gabagool with custom buy threshold
"""
from __future__ import annotations

import argparse
import signal
import sys

from market import PolymarketClient


def main() -> None:
    parser = argparse.ArgumentParser(description="BTC 15-Min Bot")
    parser.add_argument("--live", action="store_true", help="Enable live trading")
    parser.add_argument("--shares", type=float, default=6.0, help="Shares per buy (default: 6)")
    parser.add_argument("--strategy", choices=["gabagool", "straddle"], default="gabagool",
                        help="Strategy: gabagool (spread capture) or straddle (TP on oscillation)")
    parser.add_argument("--threshold", type=float, default=0.47,
                        help="Gabagool: buy when ask ≤ this (default: 0.47)")
    parser.add_argument("--max-pair", type=float, default=0.97,
                        help="Gabagool: max pair cost (default: 0.97)")
    args = parser.parse_args()

    dry_run = not args.live

    if args.live:
        print("\n⚠️  LIVE MODE — real orders will be placed!")
        print("    Press Ctrl+C to stop.\n")

    client = PolymarketClient()

    if args.strategy == "gabagool":
        from gabagool import Gabagool
        bot = Gabagool(
            client=client,
            dry_run=dry_run,
            shares=args.shares,
            buy_threshold=args.threshold,
            max_pair_cost=args.max_pair,
        )
    else:
        from straddle import Straddle
        bot = Straddle(
            client=client,
            dry_run=dry_run,
            shares=args.shares,
        )

    def on_exit(sig, frame):
        print(f"\n\n{'─' * 60}")
        print(f"  Shutting down...")
        print(f"  {bot.summary()}")
        print(f"{'─' * 60}")
        bot.running = False
        sys.exit(0)

    signal.signal(signal.SIGINT, on_exit)
    signal.signal(signal.SIGTERM, on_exit)

    try:
        bot.run()
    except KeyboardInterrupt:
        on_exit(None, None)


if __name__ == "__main__":
    main()
