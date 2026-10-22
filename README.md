# Trade-Boy

An autonomous forex trading desk run by Claude agents that debate each other before every trade.
See the [design doc](https://claude.ai/artifact/F3iKLT8hej9v6urvSDaKW9) for the full plan.

## Status

**Phase 2: the agents and their debate (dry run).** Phase 1 gave the desk its hands and brakes; phase 2
adds the brain. By default nothing is ordered: debates run and every decision is logged, so we can read
whether the agents really argue before any paper trading starts.

### The desk

| Agent | Model | Looks at |
| --- | --- | --- |
| Scanner | free code screen, Haiku only to choose between several pairs | Trend, pullbacks, range edges, breakouts on 1h and 4h |
| Technical analyst | Sonnet | Charts on 15 min, 1h, 4h and daily |
| Macro and news analyst | Sonnet + web search | Central banks, rates, data, the economic calendar |
| Positioning analyst | Sonnet + web search | OANDA retail position book, CFTC COT data |
| Skeptic | Sonnet | Everything above; argues against every trade |
| Risk manager | Sonnet | Open trades, correlation, budget used; can only shrink size |
| Head of desk | Opus | The whole debate plus each analyst's track record |

### How agreement has to be earned

The prompts tell every agent to reason rather than agree. The code enforces what can be checked:

1. **Independent first views.** The three analysts run at the same time and none sees another's view.
2. **Rebuttals.** When they disagree, each must answer the strongest argument against it. A stance change
   that doesn't name the argument behind it is thrown out (being outnumbered isn't an argument).
3. **The skeptic always objects**, rating each objection fatal, serious or minor.
4. **No unanswered objections.** If any fatal or serious objection isn't marked resolved by the head of desk,
   the outcome is no trade, whatever the head of desk decided.
5. **Risk manager can block a side** or shrink size, never grow it. Then the hard limits from phase 1 apply.
6. **Track records.** Every analyst's call is scored in pips once its own time horizon passes, and the head
   of desk sees who has actually been right.
7. **Agreement alarm.** If the analysts start on the same side in 85% or more of the last 20 debates, the
   desk raises an alert: they may be echoing each other.

### Keeping the Claude bill small

- Debates run only when the free code screen finds a setup, at most 3 a day, and not on the same pair
  twice within 4 hours. When all three analysts agree from the start, the rebuttal round is skipped.
- Spend caps: **0.90 USD per 24 hours and 20 USD per month** (`claude:` in the settings). Hitting a cap pauses
  debates; open trades keep their stop losses at OANDA.
- If the API reports the credits are gone, the agents pause, an alert goes out, and the desk retries every
  6 hours, so topping up the credits is all it takes.
- Every call's tokens and cost are journaled; `tradeboy status` shows the month so far.

### Alerts

Alerts go to the log, and to Telegram when `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set. Each kind is
sent at most once a day: budget gone (desk halted), Claude credits out, spend cap reached, agreement alarm.

### Files

- `tradeboy/oanda.py`: OANDA v20 client (account, prices, candles, position book, orders).
- `tradeboy/risk.py`: hard risk limits that every order must pass. No agent can override them.
- `tradeboy/executor.py`: runs a trade proposal through the limits, sizes it, and places it (or only logs it).
- `tradeboy/journal.py`: SQLite log of every debate, decision, trade, equity mark, agent call and API cost.
- `tradeboy/agents.py`: each agent's instructions and the exact shape of its answer.
- `tradeboy/debate.py`: one debate and the rules above.
- `tradeboy/desk.py`: the 15-minute loop, the code screen, spend caps, scoring and alarms.
- `tradeboy/llm.py`: Claude calls with structured JSON answers and cost tracking.
- `tradeboy/calendar.py`, `tradeboy/alerts.py`: economic calendar feed and alerts.

## Settings

All tunable numbers live in [`config/settings.yaml`](config/settings.yaml): the pairs traded and the risk limits.

The desk trades a **100 USD budget**, not the whole account. Profits stay in the budget and can be
traded. Once the original 100 USD is lost, the desk closes everything and halts until Mithul approves
more (`tradeboy resume --new-budget`). Percentages below are of the budget's current equity.

| Limit | Default |
| --- | --- |
| Trading budget | 100 USD, converted to SGD once at the start |
| Risk per trade | 0.5% of budget equity |
| Open trades | 3 max, one per pair |
| Total open risk | 2% of budget equity |
| Daily loss | 2% (no new trades until the 17:00 New York rollover) |
| Budget gone | Close everything, halt until `tradeboy resume --new-budget` |
| Drawdown kill switch | Off (optional `max_drawdown_pct`) |
| News blackout | 15 min either side of high-impact events (needs a calendar feed, phase 2) |
| Max spread | 3 pips |
| Min stop distance | 5 pips |

## Running

```bash
pip install -e ".[dev]"
pytest

# needs OANDA_API_TOKEN (and optionally OANDA_ACCOUNT_ID) and ANTHROPIC_API_KEY; see .env.example
python -m tradeboy check              # OANDA connection and prices
python -m tradeboy debate EUR_USD     # one debate now, printed in full (dry run)
python -m tradeboy run                # the desk, every 15 minutes, dry run
python -m tradeboy run --paper        # place winning trades on the OANDA practice account
python -m tradeboy status             # budget, spend, agreement rate, track records
```

`--paper` refuses to run against a live OANDA account. Credentials come only from environment variables.
Never commit them.
