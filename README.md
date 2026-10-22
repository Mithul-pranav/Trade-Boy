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

Every debate ends in a trade, long or short: the argument is about which side, how big and where the
stop goes, never whether to trade. The prompts tell every agent to reason rather than agree. The code
enforces what can be checked:

1. **Independent first views.** The three analysts run at the same time and none sees another's view.
2. **Rebuttals.** When they disagree, each must answer the strongest argument against it. A stance change
   that doesn't name the argument behind it is thrown out (being outnumbered isn't an argument).
3. **The skeptic always objects** to the side the analysts lean to, rating each objection fatal, serious or minor.
4. **Unanswered objections cost size.** Each fatal or serious objection the head of desk doesn't resolve halves
   the trade (down to a floor of 10% of normal risk).
5. **Size.** The risk manager sets the most each side may use (25% to 100% of normal risk), and the head of desk
   picks a share of that. Nobody can grow a trade past the normal per-trade risk.
6. **Every order has a stop.** If the head of desk's stop is on the wrong side or missing, code sets one at 1.5x
   the hourly ATR; a stop inside the 5-pip minimum is widened to it. Then the hard limits from phase 1 apply
   (per-trade risk, daily loss cap, spread, news blackout, max open trades, the 100 USD kill switch), and these
   can still stop an order.
7. **Track records.** Every analyst's call is scored in pips once its own time horizon passes, and the head
   of desk sees who has actually been right.
8. **Agreement alarm.** If the analysts start on the same side in 85% or more of the last 20 debates, the
   desk raises an alert: they may be echoing each other.

### Keeping the Claude bill small

- Debates run only when the free code screen finds a setup, at most 3 a day, and not on the same pair
  twice within 4 hours. When all three analysts agree from the start, the rebuttal round is skipped.
- Spend caps: **0.90 USD per 24 hours and 20 USD per month** (`claude:` in the settings). Hitting a cap pauses
  debates; open trades keep their stop losses at OANDA.
- If the API reports the credits are gone, the agents pause, an alert goes out, and the desk retries every
  6 hours, so topping up the credits is all it takes.
- Every call's tokens and cost are journaled; `tradeboy status` shows the month so far and the
  **net result after API costs** (trading profit or loss minus everything spent on Claude, in the
  account currency).

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
- `tradeboy/news.py`: free central bank, FX and Google News search headlines (RSS/Atom, no keys) for the
  macro analyst, so it has dated news without web search. Up to 10 per feed, each with its publisher.
  Unreachable feeds are shown to the agents as missing data.
- `tradeboy/macro.py`: rates, Treasury yields, CPI (year on year), unemployment and foreign 10-year yields
  from FRED's public CSV downloads (no key), refreshed once a day, each with the date of its reading.
- `tradeboy/sentiment.py`: optional FinBERT headline tone, run locally (`pip install -e '.[sentiment]'`,
  then `news.sentiment: true`). Off by default: torch needs about 1 GB of RAM.

The news and macro feeds need these hosts reachable from the server: news.google.com,
fred.stlouisfed.org, the central bank sites in `config/settings.yaml`, www.fxstreet.com,
nfs.faireconomy.media (calendar), and huggingface.co if FinBERT is on (one model download).

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

# needs OANDA_API_TOKEN (and optionally OANDA_ACCOUNT_ID) and TRADEBOY_ANTHROPIC_API_KEY (or ANTHROPIC_API_KEY); see .env.example
python -m tradeboy check              # OANDA connection and prices
python -m tradeboy debate EUR_USD     # one debate now, printed in full (dry run)
python -m tradeboy run                # the desk, every 15 minutes, dry run
python -m tradeboy run --paper        # place winning trades on the OANDA practice account
python -m tradeboy status             # budget, spend, agreement rate, track records
```

`--paper` refuses to run against a live OANDA account. Credentials come only from environment variables.
Never commit them.
