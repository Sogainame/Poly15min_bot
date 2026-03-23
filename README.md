# BTC 15-Minute Straddle Bot

Delta-neutral straddle strategy on Polymarket BTC Up/Down 15-minute markets.

## Strategy

1. Buy BOTH UP and DOWN tokens at start of each 15-min window (~$0.52 each)
2. Set take-profit on both sides ($0.04 = sell at ~$0.56)
3. When BTC moves → one side hits TP → sell it
4. Wait for BTC reversal → other side hits TP → sell it
5. Best case: both TP hit → +$0.04/pair net profit
6. Worst case: neither TP → lose spread only (~$0.04/pair)

## Edge

BTC oscillates within 15-minute windows. At ~0.29% 15-min volatility (54% annual),
BTC typically has a range of ~$490 per window. This creates enough price movement
for both tokens to hit modest take-profit targets.

## Usage

```bash
# Install
pip install -r requirements.txt
cp .env.example .env  # fill in your keys

# Dry run (safe)
python bot.py

# Live
python bot.py --live --shares 10 --tp 0.04
```

## Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `--shares` | 10 | Shares per side |
| `--tp` | 0.04 | Take-profit per side ($) |
| `--live` | false | Enable real trading |
