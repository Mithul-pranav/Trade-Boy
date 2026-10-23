# Trade-Boy

An autonomous trading desk run by Claude agents that debate each other before every trade.
See the [design doc](https://claude.ai/artifact/F3iKLT8hej9v6urvSDaKW9) for the original plan.

**Current focus: the tech stocks desk** (below). The forex desk that follows it is kept for reference; Mithul
dropped forex on 2026-10-23 as too risky.

## Tech stocks desk (Alpaca paper trading)

The 20 largest US tech companies (`config/stocks.yaml`), traded on an **Alpaca paper account only**: the live
trading host is not in the code. The desk trades a 1000 USD budget inside the paper account and halts when the
original 1000 USD is gone, until Mithul approves more (`tradeboy stocks resume --new-budget`).

**Debates run on events, not on a timer.** Free code checks every 10 minutes while the market is open for:
an earnings release (SEC 8-K item 2.02), a 10-Q, 10-K or material 8-K, a move of 4%+ against QQQ that is
also 2.5+ standard deviations, or a burst of headlines. At most 4 debates a day and one per stock per day.

| Seat | Who | Reads |
| --- | --- | --- |
| Filings and earnings analyst | Sonnet | The SEC filing itself (the press release for earnings), recent filings, next earnings date |
| News and sentiment analyst | Sonnet + 1 web search | Google News on the company, last 3 days |
| Technical analyst | Sonnet | Daily and hourly charts, strength against QQQ |
| Skeptic | Sonnet | Everything above; argues against the side the analysts lean to |
| Risk manager | Sonnet | Open positions, the risk quant's numbers, the next earnings date |
| Head of desk | Opus | The whole debate and every agent's track record |

The rules that make them argue rather than agree are the forex desk's, enforced in code: independent first
views, rebuttals when they disagree, stance changes must name the argument, unanswered fatal or serious
objections halve the size, an agreement alarm. Every debate ends long or short.

**The quant team is code (no Claude cost):**

- *Quant researcher* (`stocks/research.py`): post-earnings drift, tested honestly on 2008+ history (SEC
  earnings timestamps, Yahoo prices, next-open entry, ATR stop, costs, walk-forward, holdout, Holm). Only a
  rule that passes is promoted to the quant seat in debates. Anything that relies on Claude *reading* old
  news can only be tested forward: Claude already knows how those stories ended.
- *Risk quant*: correlated risk with open positions, net risk per group (semiconductors, platforms, ...)
  and the volatility regime. Its size limit is enforced in code.
- *Quant trader*: slippage of every fill against the quote at decision time, stop distance against the daily range.
- *Quant developer*: a daily health check (every position has a live stop, nothing unmanaged, no overdue
  time limits, spend caps) with a Telegram alert on problems.
- *ML researcher*: calibration of every agent's confidence (are 70% calls right 70% of the time?).

**Hard limits** (`risk:` in `config/stocks.yaml`): 1% of the budget at risk per trade, at most 4 positions,
4% total risk, one position at most 35% of the budget (an overnight gap can jump a stop), no leverage, stops
between 1% and 12%, spread under 0.25%, no entries in the first 5 or last 15 minutes, no entries within 2
trading days of earnings, 3% daily loss limit. Shorts are whole shares only (Alpaca's rule); longs can be fractional.

**Exits come first** (`stocks/exits.py`): every position has a live stop order at Alpaca (re-placed each session
for fractional shares); positions are closed the session before a scheduled earnings report, at their time
limit, or at the target; trailing stops follow 1R behind once 1R in profit; a new filing, news burst or a move
halfway to the stop triggers one cheap hold-or-close review.

```bash
# needs TRADEBOY_ALPACA_KEY_ID, TRADEBOY_ALPACA_SECRET_KEY, TRADEBOY_SEC_USER_AGENT, TRADEBOY_ANTHROPIC_API_KEY
python -m tradeboy stocks check            # Alpaca paper account, market clock, quotes
python -m tradeboy stocks events           # what the event scanner sees now (free)
python -m tradeboy stocks debate NVDA      # one debate now, printed (dry run)
python -m tradeboy stocks run --paper      # the desk on the paper account
python -m tradeboy stocks status           # budget, net after API costs, positions, track records, calibration
python -m tradeboy stocks research-download && python -m tradeboy stocks research   # the drift test
```

On a server: `bash deploy/setup_stocks.sh` installs it as the `tradeboy-stocks` service. The server needs
these hosts: paper-api.alpaca.markets, data.alpaca.markets, query1/query2.finance.yahoo.com, fc.yahoo.com,
www.sec.gov, data.sec.gov, news.google.com, api.anthropic.com, api.telegram.org.

# Forex desk (stopped 2026-10-23)

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
   (per-trade risk, daily loss cap, spread, news blackout, max open trades, the 1000 USD kill switch), and these
   can still stop an order.
7. **Track records.** Every analyst's call is scored in pips once its own time horizon passes, and the head
   of desk sees who has actually been right.
8. **The quant seat's numbers count.** It is code, not Claude, and only reports signals that proved an edge
   after costs on years of history. When it has one and disagrees, the analysts must answer it in a rebuttal
   round, and a head of desk who trades against it gets half the size. When it has none, it says so plainly.
9. **Agreement alarm.** If the analysts start on the same side in 85% or more of the last 20 debates, the
   desk raises an alert: they may be echoing each other.

### Getting out of trades

Every trade gets an exit plan when it opens, written by the head of desk and stored with the trade:
a time limit (`hold_hours`, after which code closes it), an exit type (`target`, `trailing_stop` or
`time_limit`), why the trade exists and what would make it close early. Every scan, `tradeboy/exits.py`:

- closes trades whose time limit has passed;
- for `trailing_stop` trades, moves the stop at OANDA to 1R behind price once the trade is 1R in profit
  (R = entry to the original stop), and never moves it back;
- asks one agent (Sonnet, low effort) to hold or close when something changes: price halfway to the stop,
  or a high-impact event for either currency within the hour. Each trigger is reviewed once per trade,
  and only when the API spend caps allow;
- records trades that closed at OANDA (stop or target) and reports the result.

The stop stays at OANDA the whole time, so trades are protected while the desk is offline. Telegram gets a
message when a trade opens, is re-reviewed or closes.

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

The desk trades a **1000 USD budget**, not the whole account. Profits stay in the budget and can be
traded. Once the original 1000 USD is lost, the desk closes everything and halts until Mithul approves
more (`tradeboy resume --new-budget`). Percentages below are of the budget's current equity.

| Limit | Default |
| --- | --- |
| Trading budget | 1000 USD, converted to SGD once at the start |
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

### On a server (24/7)

On a fresh Ubuntu 22.04 or 24.04 server, clone the repository and run `bash deploy/setup.sh`. It installs
everything, asks for the keys (saved only on the server, in `/etc/tradeboy.env`, readable by root only),
checks the OANDA connection and starts the desk as a `tradeboy` system service on the practice account,
restarting it after crashes and reboots. `bash deploy/update.sh` pulls the latest main and restarts it.
Logs: `data/desk.log`; stop with `sudo systemctl stop tradeboy`. Run only one desk per OANDA account.

`--paper` refuses to run against a live OANDA account. Credentials come only from environment variables.
Never commit them.
