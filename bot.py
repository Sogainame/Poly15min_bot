"""BTC 15-Minute Straddle Bot — Entry Point.

Usage:
    python bot.py                     # DRY RUN (safe, no real orders)
    python bot.py --live              # LIVE (real money!)
    python bot.py --live --shares 5   # LIVE with 5 shares per side
    python bot.py --tp 0.05           # Custom take-profit ($0.05)
"""
from __future__ import annotations

import argparse
import signal
import sys

from market import PolymarketClient
from straddle import Straddle


def main() -> None:
    parser = argparse.ArgumentParser(description="BTC 15-Min Straddle Bot")
    parser.add_argument("--live", action="store_true", help="Enable live trading")
    parser.add_argument("--shares", type=float, default=10.0, help="Shares per side (default: 10)")
    parser.add_argument("--tp", type=float, default=0.04, help="Take-profit per side in $ (default: 0.04)")
    args = parser.parse_args()

    dry_run = not args.live

    if args.live:
        print("\n⚠️  LIVE MODE — real orders will be placed!")
        print("    Press Ctrl+C to stop.\n")

    client = PolymarketClient()
    bot = Straddle(
        client=client,
        dry_run=dry_run,
        shares=args.shares,
        take_profit=args.tp,
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
