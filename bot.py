"""BTC 15-Minute Bot — Entry Point.

Strategies:
  dump_hedge — wait for dump, buy cheap, hedge when sum ≤ target (recommended)
  gabagool   — asymmetric spread capture (legacy)
  straddle   — buy both sides at open, TP on oscillation (legacy)

Usage:
    python bot.py                              # DRY dump_hedge (default)
    python bot.py --live                       # LIVE dump_hedge
    python bot.py --strategy gabagool --live   # LIVE gabagool
    python bot.py --live --sum-target 0.93     # custom sum target
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
    parser.add_argument("--strategy", choices=["dump_hedge", "gabagool", "straddle"],
                        default="dump_hedge",
                        help="Strategy (default: dump_hedge)")
    parser.add_argument("--sum-target", type=float, default=0.95,
                        help="Dump hedge: max combined cost (default: 0.95)")
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

    if args.strategy == "dump_hedge":
        from dump_hedge import DumpHedge
        bot = DumpHedge(
            client=client,
            dry_run=dry_run,
            shares=args.shares,
            sum_target=args.sum_target,
        )
    elif args.strategy == "gabagool":
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
