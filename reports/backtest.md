# Backtest results

Generated 2026-10-22T15:31+00:00 by `tradeboy backtest`. Pairs: EUR_USD, GBP_USD, USD_JPY, AUD_USD, USD_CAD, USD_CHF, NZD_USD. Hourly OANDA candles from 2016-07-03 to 2026-10-22, with real bid and ask prices.

## Verdict: NOT PASSED

Every line of the pass bar must hold before demo trading starts.

| Check | Needs | Got | |
|---|---|---|---|
| Enough out-of-sample trades | at least 300 | 0 | FAIL |
| Average profit per trade after costs | at least +0.05R | +0.000R | FAIL |
| Not luck (t-stat on monthly results) | at least 2.0 | 0.0 | FAIL |
| Profit factor (gains / losses) | at least 1.1 | 0.0 | FAIL |
| Test windows that made money | at least 60% | 0% | FAIL |
| Survives higher costs | still positive with +0.5 pips per trade | +0.000R | FAIL |
| Worst drop on the 100 USD budget | at most 20% | 0.0% | FAIL |
| Holdout year (never used to choose anything) | average above +0.00R | +0.000R over 0 trades | FAIL |
| Pays for its own debate | profit per trade above 0.15 USD API cost | +0.000 USD | FAIL |

## How to read this

- **R** is profit measured in units of the amount risked. +0.10R means that, on average, each trade made a tenth of what it risked. At the desk's 0.5% risk on 100 USD, 1R is 0.50 USD.
- **t-stat** says how sure we can be that the average is really above zero and not luck. About 2 or more is the usual bar. It is computed on monthly totals, because trades that happen together are not independent.
- **Costs** are all in: the real historical spread paid on the way in and out, 0.2 pips of slippage each way, and overnight financing (the interest rate gap, minus OANDA's 0.8% a year charge).
- A stop and a target touched in the same hour count as a stop (the worse case).

## 1. The desk's code screen, as it works today

The screen flags setups for a debate. Traded in the direction each setup implies (and, to check, in the opposite direction), with the desk's fallback stop of 1.5x the hourly ATR, a 1.5R target and a 24-hour limit:

| Strategy | Trades | Won | Average per trade | Average pips | Profit factor | t-stat |
|---|---|---|---|---|---|---|
| Code screen, natural direction | 27,467 | 36.0% | -0.150R | -2.8 | 0.76 | -16.06 |
| Code screen, opposite direction | 26,372 | 36.5% | -0.141R | -2.6 | 0.77 | -14.78 |

A coin flip between the two averages -0.145R per trade: that is roughly what costs take from every trade at this stop size.

- 100 USD run through the desk's limits (natural direction, at most 3 trades a day, 0.5% risk each): 8,467 trades, ends at 0.34 USD; half the budget was gone after 1447 trades (2018-04-09). Debating every one would have cost another 1,270 USD of Claude credits.
- 100 USD run through the desk's limits (opposite direction, at most 3 trades a day, 0.5% risk each): 8,447 trades, ends at 0.17 USD; half the budget was gone after 654 trades (2017-04-26). Debating every one would have cost another 1,267 USD of Claude credits.

## 2. Every strategy over the whole history (before the holdout year)

Fixed rules with nothing fitted. 'OPPOSITE of' rows trade against the signal. Before choosing, these are the raw numbers; a single good row here can still be luck, which is what step 3 is for.

| Strategy | Trades | Won | Average per trade | Average pips | Profit factor | t-stat |
|---|---|---|---|---|---|---|
| The desk's code screen, trading the direction each setup implies (intraday exit) | 24,819 | 36.1% | -0.147R | -2.8 | 0.76 | -14.97 |
| The desk's code screen, trading the direction each setup implies (swing exit) | 5,605 | 42.4% | -0.051R | -3.6 | 0.89 | -2.81 |
| OPPOSITE of: The desk's code screen, trading the direction each setup implies (intraday exit) | 23,783 | 36.7% | -0.136R | -2.6 | 0.78 | -14.23 |
| OPPOSITE of: The desk's code screen, trading the direction each setup implies (swing exit) | 5,578 | 43.4% | -0.038R | -3.0 | 0.92 | -2.02 |
| Code screen: 4h trend with a 1h pullback, traded with the trend (intraday exit) | 15,396 | 36.0% | -0.150R | -2.8 | 0.76 | -11.67 |
| Code screen: 4h trend with a 1h pullback, traded with the trend (swing exit) | 4,476 | 42.3% | -0.065R | -4.9 | 0.87 | -3.16 |
| OPPOSITE of: Code screen: 4h trend with a 1h pullback, traded with the trend (intraday exit) | 14,660 | 36.0% | -0.147R | -2.7 | 0.77 | -12.19 |
| OPPOSITE of: Code screen: 4h trend with a 1h pullback, traded with the trend (swing exit) | 4,711 | 43.7% | -0.030R | -2.3 | 0.94 | -1.47 |
| Code screen: price at the edge of a 4h range, traded back into the range (intraday exit) | 5,083 | 36.8% | -0.132R | -2.4 | 0.79 | -6.25 |
| Code screen: price at the edge of a 4h range, traded back into the range (swing exit) | 2,302 | 43.7% | -0.048R | -2.1 | 0.9 | -1.51 |
| OPPOSITE of: Code screen: price at the edge of a 4h range, traded back into the range (intraday exit) | 4,628 | 36.5% | -0.139R | -3.2 | 0.78 | -6.79 |
| OPPOSITE of: Code screen: price at the edge of a 4h range, traded back into the range (swing exit) | 2,156 | 43.6% | -0.045R | -5.3 | 0.9 | -1.41 |
| Code screen: 1h breakout to a 20-bar extreme, traded with the breakout (intraday exit) | 9,977 | 34.7% | -0.178R | -3.5 | 0.72 | -14.37 |
| Code screen: 1h breakout to a 20-bar extreme, traded with the breakout (swing exit) | 4,010 | 43.4% | -0.037R | -3.8 | 0.92 | -1.83 |
| OPPOSITE of: Code screen: 1h breakout to a 20-bar extreme, traded with the breakout (intraday exit) | 10,167 | 37.4% | -0.120R | -2.0 | 0.8 | -8.14 |
| OPPOSITE of: Code screen: 1h breakout to a 20-bar extreme, traded with the breakout (swing exit) | 4,294 | 43.2% | -0.060R | -3.9 | 0.87 | -2.97 |
| Time-series momentum: the direction of the last 20 trading days (intraday exit) | 43,158 | 36.7% | -0.137R | -2.8 | 0.78 | -16.09 |
| Time-series momentum: the direction of the last 20 trading days (swing exit) | 6,301 | 41.6% | -0.060R | -4.9 | 0.88 | -3.16 |
| OPPOSITE of: Time-series momentum: the direction of the last 20 trading days (intraday exit) | 43,284 | 36.7% | -0.138R | -2.7 | 0.78 | -16.9 |
| OPPOSITE of: Time-series momentum: the direction of the last 20 trading days (swing exit) | 6,273 | 41.6% | -0.058R | -3.8 | 0.88 | -3.0 |
| Time-series momentum: the direction of the last 60 trading days (intraday exit) | 43,146 | 36.8% | -0.136R | -2.7 | 0.78 | -14.13 |
| Time-series momentum: the direction of the last 60 trading days (swing exit) | 6,283 | 42.2% | -0.050R | -3.4 | 0.9 | -2.53 |
| OPPOSITE of: Time-series momentum: the direction of the last 60 trading days (intraday exit) | 43,261 | 36.7% | -0.136R | -2.8 | 0.78 | -14.28 |
| OPPOSITE of: Time-series momentum: the direction of the last 60 trading days (swing exit) | 6,250 | 42.4% | -0.048R | -3.8 | 0.9 | -2.38 |
| Time-series momentum: the direction of the last 120 trading days (intraday exit) | 43,135 | 36.8% | -0.137R | -2.9 | 0.78 | -16.02 |
| Time-series momentum: the direction of the last 120 trading days (swing exit) | 6,307 | 41.6% | -0.065R | -5.0 | 0.87 | -3.19 |
| OPPOSITE of: Time-series momentum: the direction of the last 120 trading days (intraday exit) | 43,347 | 36.6% | -0.137R | -2.6 | 0.78 | -14.78 |
| OPPOSITE of: Time-series momentum: the direction of the last 120 trading days (swing exit) | 6,222 | 42.9% | -0.030R | -2.1 | 0.94 | -1.42 |
| Daily close beyond the prior 20-day high or low, traded with the break (intraday exit) | 6,157 | 36.3% | -0.145R | -3.1 | 0.77 | -7.74 |
| Daily close beyond the prior 20-day high or low, traded with the break (swing exit) | 1,610 | 41.0% | -0.073R | -5.7 | 0.85 | -2.11 |
| OPPOSITE of: Daily close beyond the prior 20-day high or low, traded with the break (intraday exit) | 6,086 | 37.3% | -0.135R | -2.7 | 0.78 | -6.12 |
| OPPOSITE of: Daily close beyond the prior 20-day high or low, traded with the break (swing exit) | 1,830 | 44.6% | -0.011R | -0.8 | 0.98 | -0.31 |
| 1h close more than 2 standard deviations from its 24-hour average, faded (intraday exit) | 21,921 | 36.5% | -0.139R | -2.8 | 0.77 | -13.16 |
| 1h close more than 2 standard deviations from its 24-hour average, faded (swing exit) | 5,538 | 42.6% | -0.049R | -3.3 | 0.9 | -2.73 |
| OPPOSITE of: 1h close more than 2 standard deviations from its 24-hour average, faded (intraday exit) | 20,249 | 36.9% | -0.130R | -2.6 | 0.79 | -12.97 |
| OPPOSITE of: 1h close more than 2 standard deviations from its 24-hour average, faded (swing exit) | 5,483 | 43.2% | -0.038R | -2.9 | 0.92 | -1.94 |
| Carry: hold the currency with the higher interest rate (gap above 0.5 points) (intraday exit) | 28,294 | 37.5% | -0.118R | -2.4 | 0.81 | -10.48 |
| Carry: hold the currency with the higher interest rate (gap above 0.5 points) (swing exit) | 4,149 | 43.4% | -0.022R | -1.6 | 0.95 | -0.89 |
| OPPOSITE of: Carry: hold the currency with the higher interest rate (gap above 0.5 points) (intraday exit) | 28,334 | 36.1% | -0.156R | -2.9 | 0.75 | -11.0 |
| OPPOSITE of: Carry: hold the currency with the higher interest rate (gap above 0.5 points) (swing exit) | 4,202 | 40.2% | -0.078R | -5.7 | 0.85 | -3.21 |
| 1h RSI below 25 (buy) or above 75 (sell) (intraday exit) | 18,329 | 36.2% | -0.146R | -2.8 | 0.77 | -12.26 |
| 1h RSI below 25 (buy) or above 75 (sell) (swing exit) | 5,352 | 41.6% | -0.070R | -4.9 | 0.86 | -3.72 |
| OPPOSITE of: 1h RSI below 25 (buy) or above 75 (sell) (intraday exit) | 16,384 | 36.3% | -0.142R | -2.8 | 0.77 | -13.83 |
| OPPOSITE of: 1h RSI below 25 (buy) or above 75 (sell) (swing exit) | 5,242 | 43.9% | -0.028R | -2.1 | 0.94 | -1.42 |

## 3. Walk-forward (the honest test)

Each 6-month window trades only the strategies that, over the 3 years before it, had at least 60 trades, a positive average and a t-stat of at least 2.0. It is then judged on the 6 months it was not chosen on.

| Test window | Chosen strategies | Trades | Average per trade |
|---|---|---|---|
| 2019-01-01 to 2019-07-01 | none qualified | 0 | +0.000R |
| 2019-07-01 to 2020-01-01 | none qualified | 0 | +0.000R |
| 2020-01-01 to 2020-07-01 | none qualified | 0 | +0.000R |
| 2020-07-01 to 2021-01-01 | none qualified | 0 | +0.000R |
| 2021-01-01 to 2021-07-01 | none qualified | 0 | +0.000R |
| 2021-07-01 to 2022-01-01 | none qualified | 0 | +0.000R |
| 2022-01-01 to 2022-07-01 | none qualified | 0 | +0.000R |
| 2022-07-01 to 2023-01-01 | none qualified | 0 | +0.000R |
| 2023-01-01 to 2023-07-01 | none qualified | 0 | +0.000R |
| 2023-07-01 to 2024-01-01 | none qualified | 0 | +0.000R |
| 2024-01-01 to 2024-07-01 | none qualified | 0 | +0.000R |
| 2024-07-01 to 2025-01-01 | none qualified | 0 | +0.000R |
| 2025-01-01 to 2025-07-01 | none qualified | 0 | +0.000R |
| 2025-07-01 to 2025-10-22 | none qualified | 0 | +0.000R |

No strategy qualified in any window, so there were no out-of-sample trades to judge.

## 4. Holdout year

From 2025-10-22 to 2026-10-22, with strategies chosen on the 3 years before: none qualified. 0 trades, +0.000R on average.

## What the quant seat does with this

The quant seat only leans on strategies that pass this whole process. Until the pass bar is met it tells the debate, in plain words, that it has no tested edge, and its side carries no weight.
