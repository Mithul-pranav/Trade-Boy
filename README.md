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
| Quant seat | none: plain code, free | Statistical signals that passed the backtest (see below) |

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
8. **The quant seat's numbers count.** It is code, not Claude, and only reports signals that proved an edge
   after costs on years of history. When it has one and disagrees, the analysts must answer it in a rebuttal
   round, and a head of desk who trades against it gets half the size. When it has none, it says so plainly.
9. **Agreement alarm.** If the analysts start on the same side in 85% or more of the last 20 debates, the
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

Alerts go to the log, and to Telegram when `TRADEBOY_TELEGRAM_BOT_TOKEN` is set. Each kind is sent at most
once a day: budget gone (desk halted), Claude credits out, spend cap reached, agreement alarm.

To set up Telegram: create a bot with @BotFather, set its token as `TRADEBOY_TELEGRAM_BOT_TOKEN`, send the bot
any message, then run `python -m tradeboy test-alert`. The chat ID is found from that message and remembered;
`TRADEBOY_TELEGRAM_CHAT_ID` can pin it instead. The desk's server must be able to reach `api.telegram.org`.

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
- `tradeboy/quant.py`: the quant seat: statistical signals computed in code, and its view in a debate.
- `tradeboy/history.py`: downloads and loads OANDA history with bid/ask prices, and FRED short-term rates.
- `tradeboy/backtest.py`: the backtester, walk-forward, pass bar and report.
- `tradeboy/sentiment.py`: optional FinBERT headline tone, run locally (`pip install -e '.[sentiment]'`,
  then `news.sentiment: true`). Off by default: torch needs about 1 GB of RAM.

The news and macro feeds need these hosts reachable from the server: news.google.com,
fred.stlouisfed.org, the central bank sites in `config/settings.yaml`,
nfs.faireconomy.media (calendar), and huggingface.co if FinBERT is on (one model download).

## Backtesting and the pass bar

Before any demo trading, the desk's code-level rules must pass a written bar on years of real history.
`tradeboy history` downloads hourly, 4-hour and daily OANDA candles with bid and ask prices for the 7 pairs
(2016 onwards, about 470,000 hourly candles) plus each currency's 3-month interest rate from FRED.
`tradeboy backtest` then tests:

- **The code screen as the desk uses it**, in the direction each setup implies and the opposite one.
- **11 statistical signals** (momentum over 20/60/120 days, daily breakouts, carry, 1h stretch and RSI
  fades, and each part of the code screen), each traded both ways with two exit styles: 44 strategies.

Costs are real: the historical spread both ways, 0.2 pips of slippage per side, and overnight financing
(the interest rate gap minus OANDA's charge). The verdict comes only from **walk-forward** test windows: a
strategy trades in a 6-month window only if it showed an edge in the 3 years before. The last 12 months are
a **holdout** checked once. Claude debates are not replayed (each costs about 0.15 USD); the rules are.

The pass bar (`backtest.pass_bar` in the settings) needs, out of sample: 300+ trades, at least +0.05R per
trade after costs, a t-stat of 2 or more, profit factor 1.1+, 60% of windows positive, still positive with
0.5 pips more cost, a worst drop under 20% of the budget, a positive holdout year, and profit per trade at
0.5% risk that covers one debate's API cost.

**Latest result: not passed.** See [reports/backtest.md](reports/backtest.md). In short, no rule or signal
showed an edge after costs; the code screen loses about 0.15R per trade in either direction, which is the
cost of trading itself. The quant seat therefore tells every debate it has no tested edge.

### Slow macro strategies (weekly)

`tradeboy macro-history` then `tradeboy macro-backtest` (needs `pip install -e ".[research]"`) test slower,
interest-rate driven ideas on weekly positions since 2004: carry, carry with a trend filter, short-rate and
10-year yield spread momentum, central bank rate moves, 5-year value, a carry/trend/value mix and a ridge
regression. It also compares seven exit rules on the same entries (signal flip, protective stop, central bank
move, fixed stop and target, trailing stop, 4 and 13 week time exits), with random entries as a control.

**Latest result: not passed.** See [reports/macro_backtest.md](reports/macro_backtest.md). No rule and no exit
showed an edge after costs out of sample, exits on random entries lose about the cost of trading, and even a
real modest edge would not pay for weekly debates on a 100 USD budget. The report ends with how the desk
enters and exits today and a proposal for managing open trades.

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
python -m tradeboy history            # download years of candles and interest rates (once, then to top up)
python -m tradeboy backtest           # test the rules, check the pass bar, write reports/backtest.md
python -m tradeboy macro-history      # daily/4h candles since 2004, FRED rates, central bank decisions
python -m tradeboy macro-backtest     # weekly macro strategies and exit rules, write reports/macro_backtest.md
```

`--paper` refuses to run against a live OANDA account. Credentials come only from environment variables.
Never commit them.
