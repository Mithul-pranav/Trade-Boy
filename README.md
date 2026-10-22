# Trade-Boy

An autonomous forex trading desk run by Claude agents that debate each other before every trade.
See the [design doc](https://claude.ai/artifact/F3iKLT8hej9v6urvSDaKW9) for the full plan.

## Status

**Phase 1: foundation.** No agents yet. This phase gives the desk its hands and its brakes:

- `tradeboy/oanda.py`: OANDA v20 client (account, live prices, candles, market orders with stop loss).
- `tradeboy/risk.py`: hard risk limits that every order must pass. No agent can override them.
- `tradeboy/executor.py`: runs a trade proposal through the limits, sizes it, and places it (or only logs it in dry-run mode).
- `tradeboy/journal.py`: SQLite log of every decision, trade and equity mark.

## Settings

All tunable numbers live in [`config/settings.yaml`](config/settings.yaml): the pairs traded and the risk limits.

| Limit | Default |
| --- | --- |
| Risk per trade | 0.5% of NAV |
| Open trades | 3 max, one per pair |
| Total open risk | 2% of NAV |
| Daily loss | 2% (no new trades until the 17:00 New York rollover) |
| Drawdown kill switch | 10% from peak: close everything, halt until `tradeboy resume` |
| News blackout | 15 min either side of high-impact events (needs a calendar feed, phase 2) |
| Max spread | 3 pips |

## Running

```bash
pip install -e ".[dev]"
pytest

# needs OANDA_API_TOKEN and OANDA_ACCOUNT_ID in the environment (see .env.example)
python -m tradeboy check
```

Credentials come only from environment variables. Never commit them.
