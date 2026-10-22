# Macro strategy backtest (weekly, 2004 onwards)

Generated 2026-10-22T15:49+00:00 by `tradeboy macro-backtest`. Pairs: EUR_USD, GBP_USD, USD_JPY, AUD_USD, USD_CAD, USD_CHF, NZD_USD. Weekly positions from 2004-01-05 to 2026-10-12, executed at the first 4h OANDA candle from Monday 09:00 UTC with that candle's real bid and ask.

## Verdict: NOT PASSED

| Check | Needs | Got | |
|---|---|---|---|
| Out-of-sample weeks with positions | at least 300 | 520 | pass |
| Sharpe ratio after costs (out of sample) | at least 0.5 | -0.59 | FAIL |
| Not luck (t-stat, out of sample) | at least 2.0 | -2.33 | FAIL |
| Test windows that made money | at least 60% | 30% | FAIL |
| Survives higher costs (2x spread, 0.5 pip slippage, 1.5% financing markup) | Sharpe above 0 | -0.6 | FAIL |
| Worst drop at 10% yearly volatility | at most 20% | 63.3% | FAIL |
| Holdout year (never used to choose anything) | above 0% | 0.0% (nothing chosen) | FAIL |
| Pays for a weekly portfolio debate on 100 USD | profit above 7.8 USD a year | -5.85 USD | FAIL |

## How to read this

- **Sharpe ratio** is yearly return divided by yearly ups and downs. 0 means no edge; 0.5 is a modest real edge; a good macro hedge fund aims for about 1.
- **t-stat** says how sure we can be that the average is above zero and not luck; about 2 or more is the usual bar. **Holm p** corrects for testing many strategies at once: with 55 strategies, some will look good by chance. Below 0.05 would count.
- **At 10% volatility** means the strategy scaled so its yearly ups and downs are about 10% of capital. On 100 USD that is roughly a 10 USD swing in a typical year.
- **Costs** are all in: half the real spread each time a position changes, 0.2 pips slippage each side, and overnight financing (the interest rate gap minus OANDA's 0.8% a year).
- Positions are sized so each pair has the same risk (10% yearly volatility on its share of capital).
- Rates: OECD monthly 3-month and 10-year rates on FRED, used only from the month after next. Where a 3-month series is more than 100 days stale, the daily policy rate is used (Fed, ECB, BoE, RBA, BoC). Two-year yields for all 8 currencies are not available free, so 3-month rates stand in for short-term rate expectations.

## 1. Every strategy over the development years (2006 to October 2025)

Fixed rules, nothing fitted (except the ridge regression, which is refitted only on past data). 'Exit' variants hold one trade at a time per pair and book each trade in the week it closes.

| Strategy | Sharpe | t-stat | Holm p | Yearly at 10% vol | Worst drop | Higher costs Sharpe | 2006-09 | 2010-14 | 2015-19 | 2020-25 |
|---|---|---|---|---|---|---|---|---|---|---|
| 10-year yield gap: hold the currency with the higher 10-year yield; exit: trailing stop 2 weekly moves behind the best price, at most 26 weeks | +0.20 | +0.88 | 1.00 | +2.0% | 27% | +0.00 | -0.11 | +0.15 | +0.11 | +0.51 |
| 10-year yield gap: hold the currency with the higher 10-year yield | +0.14 | +0.64 | 1.00 | +1.4% | 34% | -0.00 | -0.55 | +0.78 | +0.15 | +0.28 |
| 10-year yield gap: hold the currency with the higher 10-year yield; exit: close after 4 weeks, whatever happens | +0.13 | +0.57 | 1.00 | +1.3% | 34% | -0.03 | -0.59 | +0.64 | +0.18 | +0.28 |
| 10-year yield gap: hold the currency with the higher 10-year yield; exit: close after 13 weeks, whatever happens | +0.12 | +0.55 | 1.00 | +1.2% | 42% | -0.03 | -0.64 | +0.57 | +0.32 | +0.46 |
| Policy cycle: side with the central bank whose last rate move was a hike; exit: close after 13 weeks, whatever happens | +0.10 | +0.44 | 1.00 | +1.0% | 41% | -0.05 | -0.14 | +0.40 | +0.02 | +0.21 |
| Short-rate momentum over 26 weeks | +0.06 | +0.28 | 1.00 | +0.6% | 42% | -0.05 | -0.14 | +0.03 | +0.14 | +0.19 |
| 10-year yield spread momentum over 13 weeks | +0.05 | +0.20 | 1.00 | +0.4% | 41% | +0.08 | +0.63 | +0.28 | -0.04 | -0.59 |
| Price trend: direction of the last 26 weeks; exit: close after 4 weeks, whatever happens | +0.02 | +0.10 | 1.00 | +0.2% | 46% | -0.10 | +1.20 | -0.09 | -0.20 | -0.54 |
| Price trend: direction of the last 26 weeks; exit: signal flip, plus a protective stop 2 weekly moves away | +0.01 | +0.03 | 1.00 | +0.1% | 49% | -0.22 | +0.94 | -0.91 | +0.45 | -0.81 |
| Policy cycle: side with the central bank whose last rate move was a hike; exit: signal flip, plus a protective stop 2 weekly moves away | +0.00 | +0.01 | 1.00 | +0.0% | 33% | -0.22 | -0.18 | +0.23 | -0.01 | +0.05 |
| 10-year yield gap: hold the currency with the higher 10-year yield; exit: signal flip, or a central bank rate move against the trade | +0.00 | +0.00 | 1.00 | +0.0% | 47% | -0.12 | -1.29 | +0.61 | +0.80 | -0.03 |
| Policy cycle: side with the central bank whose last rate move was a hike; exit: trailing stop 2 weekly moves behind the best price, at most 26 weeks | -0.00 | -0.01 | 1.00 | -0.0% | 49% | -0.17 | +0.18 | +0.34 | -0.53 | +0.11 |
| Short-rate momentum over 26 weeks; exit: signal flip, plus a protective stop 2 weekly moves away | -0.01 | -0.04 | 1.00 | -0.1% | 39% | -0.28 | -0.16 | +0.01 | -0.04 | +0.08 |
| Short-rate momentum over 26 weeks; exit: close after 4 weeks, whatever happens | -0.01 | -0.04 | 1.00 | -0.1% | 49% | -0.17 | -0.22 | -0.08 | +0.06 | +0.18 |
| 10-year yield gap: hold the currency with the higher 10-year yield; exit: fixed stop 2 weekly moves away, target at 2x the stop, at most 13 weeks | -0.01 | -0.04 | 1.00 | -0.1% | 32% | -0.19 | -0.19 | +0.25 | -0.13 | +0.00 |
| Policy cycle: side with the central bank whose last rate move was a hike | -0.03 | -0.14 | 1.00 | -0.3% | 38% | -0.12 | -0.29 | +0.68 | -0.07 | -0.04 |
| Price trend: direction of the last 26 weeks; exit: signal flip, or a central bank rate move against the trade | -0.03 | -0.15 | 1.00 | -0.3% | 43% | -0.17 | +0.64 | -0.75 | +0.41 | -0.72 |
| Carry, only when the 26-week trend agrees (flat otherwise); exit: trailing stop 2 weekly moves behind the best price, at most 26 weeks | -0.04 | -0.16 | 1.00 | -0.4% | 52% | -0.10 | +0.97 | -1.01 | -0.51 | +0.26 |
| Short-rate momentum over 26 weeks; exit: fixed stop 2 weekly moves away, target at 2x the stop, at most 13 weeks | -0.05 | -0.22 | 1.00 | -0.5% | 55% | -0.17 | -0.13 | -0.30 | -0.15 | +0.24 |
| Price trend: direction of the last 26 weeks | -0.06 | -0.27 | 1.00 | -0.6% | 44% | -0.16 | +0.72 | -0.11 | -0.28 | -0.49 |
| Policy cycle: side with the central bank whose last rate move was a hike; exit: fixed stop 2 weekly moves away, target at 2x the stop, at most 13 weeks | -0.06 | -0.27 | 1.00 | -0.6% | 49% | -0.18 | -0.05 | +0.48 | -0.33 | -0.12 |
| Policy cycle: side with the central bank whose last rate move was a hike; exit: close after 4 weeks, whatever happens | -0.07 | -0.29 | 1.00 | -0.7% | 43% | -0.19 | -0.34 | +0.59 | -0.19 | +0.03 |
| Short-rate momentum over 26 weeks; exit: trailing stop 2 weekly moves behind the best price, at most 26 weeks | -0.07 | -0.31 | 1.00 | -0.7% | 60% | -0.23 | +0.14 | -0.50 | -0.43 | +0.28 |
| Short-rate momentum over 26 weeks; exit: close after 13 weeks, whatever happens | -0.07 | -0.31 | 1.00 | -0.7% | 55% | -0.20 | -0.30 | -0.29 | +0.20 | +0.02 |
| Carry, only when the 26-week trend agrees (flat otherwise) | -0.07 | -0.33 | 1.00 | -0.7% | 38% | -0.09 | +0.46 | -0.48 | -0.30 | +0.06 |
| Short-rate momentum over 26 weeks; exit: signal flip, or a central bank rate move against the trade | -0.08 | -0.34 | 1.00 | -0.8% | 38% | -0.25 | -0.37 | -0.22 | -0.17 | +0.14 |
| Policy cycle: side with the central bank whose last rate move was a hike; exit: signal flip, or a central bank rate move against the trade | -0.08 | -0.37 | 1.00 | -0.8% | 29% | -0.21 | -0.86 | +0.22 | -0.00 | +0.10 |
| Carry, only when the 26-week trend agrees (flat otherwise); exit: close after 13 weeks, whatever happens | -0.09 | -0.38 | 1.00 | -0.9% | 48% | -0.16 | -0.09 | -0.71 | -0.06 | +0.39 |
| Short-rate momentum: side with the currency whose 3-month rate rose more over 13 weeks | -0.09 | -0.39 | 1.00 | -0.9% | 40% | -0.18 | +0.11 | -0.27 | +0.27 | -0.43 |
| Carry, only when the 26-week trend agrees (flat otherwise); exit: signal flip, plus a protective stop 2 weekly moves away | -0.09 | -0.41 | 1.00 | -0.9% | 51% | -0.21 | +0.66 | -0.97 | -0.81 | +0.08 |
| Carry, only when the 26-week trend agrees (flat otherwise); exit: fixed stop 2 weekly moves away, target at 2x the stop, at most 13 weeks | -0.10 | -0.46 | 1.00 | -1.0% | 50% | -0.15 | +0.65 | -0.44 | -0.70 | +0.14 |
| Price trend: direction of the last 52 weeks | -0.11 | -0.49 | 1.00 | -1.1% | 44% | -0.26 | +0.26 | -0.42 | -0.32 | -0.02 |
| Carry, only when the 26-week trend agrees (flat otherwise); exit: close after 4 weeks, whatever happens | -0.12 | -0.53 | 1.00 | -1.2% | 47% | -0.12 | +0.55 | -0.83 | -0.36 | +0.05 |
| Carry: hold the higher-yielding currency (3-month rates); exit: trailing stop 2 weekly moves behind the best price, at most 26 weeks | -0.12 | -0.55 | 1.00 | -1.2% | 61% | -0.25 | -0.12 | -0.85 | -0.30 | +0.57 |
| Price trend: direction of the last 26 weeks; exit: trailing stop 2 weekly moves behind the best price, at most 26 weeks | -0.12 | -0.55 | 1.00 | -1.2% | 59% | -0.25 | +1.04 | -0.65 | -0.55 | -0.49 |
| 10-year yield gap: hold the currency with the higher 10-year yield; exit: signal flip, plus a protective stop 2 weekly moves away | -0.13 | -0.59 | 1.00 | -1.3% | 51% | -0.48 | -1.12 | -0.28 | +0.60 | -0.03 |
| Price trend: direction of the last 26 weeks; exit: close after 13 weeks, whatever happens | -0.13 | -0.59 | 1.00 | -1.3% | 40% | -0.32 | +0.19 | -0.24 | -0.30 | -0.23 |
| Price trend: direction of the last 13 weeks | -0.14 | -0.63 | 1.00 | -1.4% | 38% | -0.35 | +0.04 | +0.12 | -0.50 | -0.27 |
| Price trend: direction of the last 26 weeks; exit: fixed stop 2 weekly moves away, target at 2x the stop, at most 13 weeks | -0.14 | -0.64 | 1.00 | -1.4% | 53% | -0.29 | +0.62 | -0.21 | -0.63 | -0.40 |
| Carry, only when the 26-week trend agrees (flat otherwise); exit: signal flip, or a central bank rate move against the trade | -0.16 | -0.72 | 1.00 | -1.6% | 50% | -0.22 | +0.74 | -0.89 | -0.36 | -0.07 |
| Mix: majority vote of carry, 26-week trend and value; exit: trailing stop 2 weekly moves behind the best price, at most 26 weeks | -0.18 | -0.80 | 1.00 | -1.8% | 61% | -0.31 | +1.06 | -1.20 | -0.52 | +0.04 |
| Value: bet on a return toward the 5-year average price | -0.20 | -0.91 | 1.00 | -2.0% | 49% | -0.33 | +0.25 | -0.74 | -0.09 | -0.26 |
| Carry: hold the higher-yielding currency (3-month rates) | -0.22 | -0.96 | 1.00 | -2.2% | 59% | -0.29 | -0.50 | -0.76 | -0.34 | +0.45 |
| Carry: hold the higher-yielding currency (3-month rates); exit: fixed stop 2 weekly moves away, target at 2x the stop, at most 13 weeks | -0.23 | -1.02 | 1.00 | -2.3% | 64% | -0.37 | -0.26 | -0.88 | -0.34 | +0.36 |
| Mix: majority vote of carry, 26-week trend and value; exit: fixed stop 2 weekly moves away, target at 2x the stop, at most 13 weeks | -0.23 | -1.02 | 1.00 | -2.3% | 65% | -0.35 | +0.98 | -1.21 | -0.51 | +0.12 |
| Mix: majority vote of carry, 26-week trend and value; exit: signal flip, plus a protective stop 2 weekly moves away | -0.24 | -1.06 | 1.00 | -2.4% | 64% | -0.41 | +0.53 | -1.05 | -0.17 | -0.17 |
| Mix: majority vote of carry, 26-week trend and value | -0.24 | -1.08 | 1.00 | -2.4% | 55% | -0.33 | +0.48 | -1.00 | -0.32 | -0.07 |
| Mix: majority vote of carry, 26-week trend and value; exit: close after 13 weeks, whatever happens | -0.27 | -1.22 | 1.00 | -2.7% | 60% | -0.39 | -0.22 | -0.54 | -0.88 | +0.29 |
| Carry: hold the higher-yielding currency (3-month rates); exit: close after 4 weeks, whatever happens | -0.29 | -1.27 | 1.00 | -2.9% | 63% | -0.38 | -0.51 | -0.86 | -0.47 | +0.40 |
| Carry: hold the higher-yielding currency (3-month rates); exit: close after 13 weeks, whatever happens | -0.30 | -1.32 | 1.00 | -2.9% | 65% | -0.36 | -0.57 | -0.67 | -0.27 | +0.36 |
| Mix: majority vote of carry, 26-week trend and value; exit: signal flip, or a central bank rate move against the trade | -0.32 | -1.42 | 1.00 | -3.2% | 59% | -0.42 | +0.36 | -0.29 | -0.58 | -0.42 |
| Carry: hold the higher-yielding currency (3-month rates); exit: signal flip, or a central bank rate move against the trade | -0.37 | -1.67 | 1.00 | -3.7% | 61% | -0.40 | -1.29 | -0.31 | -0.44 | +0.09 |
| Ridge regression on all features (machine learning, refitted every 6 months) | -0.49 | -2.17 | 1.00 | -4.9% | 66% | -0.63 | +0.00 | -0.02 | -0.82 | -0.69 |
| Carry: hold the higher-yielding currency (3-month rates); exit: signal flip, plus a protective stop 2 weekly moves away | -0.49 | -2.20 | 1.00 | -4.9% | 76% | -0.75 | -0.96 | -2.27 | -0.11 | +0.21 |
| Mix: majority vote of carry, 26-week trend and value; exit: close after 4 weeks, whatever happens | -0.52 | -2.32 | 1.00 | -5.2% | 76% | -0.58 | +0.62 | -1.67 | -0.41 | -0.56 |

Best by t-stat: **10-year yield gap: hold the currency with the higher 10-year yield; exit: trailing stop 2 weekly moves behind the best price, at most 26 weeks** (t = 0.88, Holm p = 1.00). The Holm p says whether that is more than the luck expected from trying 55 strategies.

## 2. Exits, head to head on the same entries

Each rule's entries are the weeks its signal switches on or changes side. Every exit rule is applied to exactly those entries, so the only difference is the exit. Results are per trade, in **R**, where 1R is two typical weekly moves (the stop distance used by the stop exits). Random entries (a coin flip for the side, about one new trade every 8 weeks per pair, 10 different coins) show what exits do when the entry has no edge.

| Entries | Exit | Trades | Average per trade | t-stat | Won | Average hold (weeks) |
|---|---|---|---|---|---|---|
| carry | Hold until the weekly signal no longer agrees | 54 | -1.484R | -2.10 | 44% | 64.3 |
| carry | Signal flip, plus a protective stop 2 weekly moves away | 57 | -0.141R | -0.56 | 32% | 16.7 |
| carry | Signal flip, or a central bank rate move against the trade | 60 | -0.804R | -1.71 | 43% | 36.4 |
| carry | Fixed stop 2 weekly moves away, target at 2x the stop, at most 13 weeks | 60 | -0.009R | -0.06 | 38% | 6.5 |
| carry | Trailing stop 2 weekly moves behind the best price, at most 26 weeks | 60 | -0.132R | -1.21 | 32% | 3.9 |
| carry | Close after 4 weeks, whatever happens | 60 | -0.232R | -1.54 | 43% | 4.0 |
| carry | Close after 13 weeks, whatever happens | 60 | +0.167R | +0.61 | 57% | 13.0 |
| carry_trend | Hold until the weekly signal no longer agrees | 388 | -0.041R | -0.46 | 41% | 9.8 |
| carry_trend | Signal flip, plus a protective stop 2 weekly moves away | 388 | +0.044R | +0.54 | 38% | 8.6 |
| carry_trend | Signal flip, or a central bank rate move against the trade | 388 | -0.037R | -0.46 | 43% | 8.0 |
| carry_trend | Fixed stop 2 weekly moves away, target at 2x the stop, at most 13 weeks | 388 | +0.179R | +2.68 | 45% | 6.7 |
| carry_trend | Trailing stop 2 weekly moves behind the best price, at most 26 weeks | 389 | +0.009R | +0.18 | 38% | 4.1 |
| carry_trend | Close after 4 weeks, whatever happens | 388 | +0.033R | +0.55 | 54% | 4.0 |
| carry_trend | Close after 13 weeks, whatever happens | 388 | +0.102R | +0.87 | 57% | 13.0 |
| rate_mom_26w | Hold until the weekly signal no longer agrees | 174 | -0.128R | -0.61 | 48% | 32.9 |
| rate_mom_26w | Signal flip, plus a protective stop 2 weekly moves away | 178 | -0.175R | -1.33 | 30% | 12.4 |
| rate_mom_26w | Signal flip, or a central bank rate move against the trade | 179 | +0.004R | +0.02 | 48% | 26.2 |
| rate_mom_26w | Fixed stop 2 weekly moves away, target at 2x the stop, at most 13 weeks | 179 | -0.005R | -0.06 | 38% | 6.3 |
| rate_mom_26w | Trailing stop 2 weekly moves behind the best price, at most 26 weeks | 179 | -0.082R | -1.11 | 34% | 3.7 |
| rate_mom_26w | Close after 4 weeks, whatever happens | 179 | +0.057R | +0.66 | 50% | 4.0 |
| rate_mom_26w | Close after 13 weeks, whatever happens | 179 | -0.022R | -0.15 | 45% | 13.0 |
| yield_level | Hold until the weekly signal no longer agrees | 55 | -0.283R | -0.53 | 49% | 49.1 |
| yield_level | Signal flip, plus a protective stop 2 weekly moves away | 60 | +0.030R | +0.13 | 32% | 20.0 |
| yield_level | Signal flip, or a central bank rate move against the trade | 59 | +0.174R | +0.41 | 51% | 28.0 |
| yield_level | Fixed stop 2 weekly moves away, target at 2x the stop, at most 13 weeks | 60 | -0.130R | -0.86 | 32% | 7.5 |
| yield_level | Trailing stop 2 weekly moves behind the best price, at most 26 weeks | 60 | -0.111R | -1.02 | 32% | 5.3 |
| yield_level | Close after 4 weeks, whatever happens | 59 | +0.158R | +1.29 | 58% | 4.0 |
| yield_level | Close after 13 weeks, whatever happens | 59 | -0.059R | -0.29 | 44% | 13.0 |
| trend_26w | Hold until the weekly signal no longer agrees | 717 | -0.014R | -0.18 | 34% | 10.7 |
| trend_26w | Signal flip, plus a protective stop 2 weekly moves away | 719 | +0.030R | +0.42 | 31% | 9.3 |
| trend_26w | Signal flip, or a central bank rate move against the trade | 720 | +0.019R | +0.27 | 37% | 8.5 |
| trend_26w | Fixed stop 2 weekly moves away, target at 2x the stop, at most 13 weeks | 721 | +0.026R | +0.54 | 41% | 6.3 |
| trend_26w | Trailing stop 2 weekly moves behind the best price, at most 26 weeks | 721 | -0.024R | -0.61 | 34% | 3.9 |
| trend_26w | Close after 4 weeks, whatever happens | 721 | -0.025R | -0.56 | 48% | 4.0 |
| trend_26w | Close after 13 weeks, whatever happens | 721 | -0.024R | -0.26 | 51% | 13.0 |
| policy_cycle | Hold until the weekly signal no longer agrees | 74 | -0.280R | -0.60 | 39% | 50.4 |
| policy_cycle | Signal flip, plus a protective stop 2 weekly moves away | 76 | -0.371R | -1.51 | 20% | 15.1 |
| policy_cycle | Signal flip, or a central bank rate move against the trade | 74 | -0.178R | -0.39 | 39% | 50.1 |
| policy_cycle | Fixed stop 2 weekly moves away, target at 2x the stop, at most 13 weeks | 76 | -0.265R | -1.89 | 30% | 6.8 |
| policy_cycle | Trailing stop 2 weekly moves behind the best price, at most 26 weeks | 76 | -0.195R | -2.17 | 26% | 4.0 |
| policy_cycle | Close after 4 weeks, whatever happens | 77 | -0.140R | -1.27 | 40% | 4.0 |
| policy_cycle | Close after 13 weeks, whatever happens | 74 | -0.226R | -1.00 | 39% | 13.0 |
| carry_trend_value | Hold until the weekly signal no longer agrees | 533 | -0.057R | -0.53 | 45% | 12.7 |
| carry_trend_value | Signal flip, plus a protective stop 2 weekly moves away | 535 | +0.020R | +0.34 | 39% | 6.5 |
| carry_trend_value | Signal flip, or a central bank rate move against the trade | 537 | -0.114R | -1.40 | 45% | 8.4 |
| carry_trend_value | Fixed stop 2 weekly moves away, target at 2x the stop, at most 13 weeks | 537 | +0.021R | +0.37 | 40% | 6.3 |
| carry_trend_value | Trailing stop 2 weekly moves behind the best price, at most 26 weeks | 537 | +0.009R | +0.19 | 37% | 4.0 |
| carry_trend_value | Close after 4 weeks, whatever happens | 537 | -0.016R | -0.31 | 50% | 4.0 |
| carry_trend_value | Close after 13 weeks, whatever happens | 537 | -0.048R | -0.42 | 51% | 13.0 |
| random | Hold until the weekly signal no longer agrees | 979 | -0.066R (coins ranged -0.212R to +0.040R) | -0.92 | 49% | 14.9 |
| random | Signal flip, or a central bank rate move against the trade | 987 | -0.043R (coins ranged -0.127R to +0.133R) | -0.71 | 48% | 11.2 |
| random | Signal flip, plus a protective stop 2 weekly moves away | 984 | -0.038R (coins ranged -0.124R to +0.027R) | -0.80 | 36% | 7.6 |
| random | Fixed stop 2 weekly moves away, target at 2x the stop, at most 13 weeks | 984 | -0.036R (coins ranged -0.125R to +0.040R) | -0.90 | 38% | 6.1 |
| random | Close after 13 weeks, whatever happens | 981 | -0.064R (coins ranged -0.162R to +0.012R) | -0.93 | 48% | 13.0 |
| random | Close after 4 weeks, whatever happens | 990 | -0.041R (coins ranged -0.083R to +0.023R) | -1.09 | 48% | 4.0 |
| random | Trailing stop 2 weekly moves behind the best price, at most 26 weeks | 987 | -0.035R (coins ranged -0.076R to +0.017R) | -1.15 | 35% | 3.7 |

The strongest result here, carry_trend with the 'stop_target' exit (t = +2.68), is the best of 49 rule and exit pairs, so a t-stat that high is expected from luck alone about 18% of the time. Traded for real (one position per pair at a time, section 1) it has a Sharpe of -0.10.

With random entries every exit loses a little: flip -0.066R, flip_policy -0.043R, flip_stop -0.038R, stop_target -0.036R, time_13w -0.064R, time_4w -0.041R, trailing -0.035R. An exit changes how often you win and how big wins and losses are, but on its own it does not create an edge; it can only keep more of one the entry already has.

## 3. Central bank rate moves

240 rate changes by the Fed, ECB, BoE, RBA and BoC since 2004 (daily records for the BoJ, SNB and RBNZ are not reachable from here). After each one, trade the currency in the direction of the move against the dollar from the next Monday, after costs, in units of the hold period's typical move:

| Hold | Events | Average | Won | t-stat |
|---|---|---|---|---|
| 1 week(s) | 228 | +0.057 | 50.4% | +0.69 |
| 2 week(s) | 228 | +0.034 | 48.7% | +0.35 |
| 4 week(s) | 228 | -0.093 | 43.9% | -1.22 |

Rate moves are mostly expected by the market before they happen, so the move itself carries little news. The surprise (the move versus what was priced) is what moves currencies, and testing that needs market pricing data that is not free.

## 4. Walk-forward (the honest test)

Every 6 months, trade the strategies whose t-stat over all earlier weeks (at least 5 years) was at least 2.0, then judge them on the 6 months they were not chosen on. Strategies in the pool: 55.

| Test window | Chosen strategies | Return at 10% vol |
|---|---|---|
| 2010-01-01 to 2010-07-01 | yield_mom_13w, carry_trend+trailing, carry_trend+time_4w, trend_26w+trailing, trend_26w+time_4w, carry_trend_value+stop_target, carry_trend_value+trailing, carry_trend_value+time_4w | -11.9% |
| 2010-07-01 to 2011-01-01 | yield_mom_13w, trend_26w+time_4w | +5.2% |
| 2011-01-01 to 2011-07-01 | yield_mom_13w | -9.5% |
| 2011-07-01 to 2012-01-01 | yield_mom_13w, trend_26w+time_4w | +4.0% |
| 2012-01-01 to 2012-07-01 | yield_mom_13w | -2.5% |
| 2012-07-01 to 2013-01-01 | yield_mom_13w | +2.9% |
| 2013-01-01 to 2013-07-01 | yield_mom_13w | +0.2% |
| 2013-07-01 to 2014-01-01 | yield_mom_13w | -3.8% |
| 2014-01-01 to 2014-07-01 | yield_mom_13w | -0.6% |
| 2014-07-01 to 2015-01-01 | yield_mom_13w | +4.3% |
| 2015-01-01 to 2015-07-01 | yield_mom_13w | +2.9% |
| 2015-07-01 to 2016-01-01 | yield_mom_13w | -4.3% |
| 2016-01-01 to 2016-07-01 | yield_mom_13w | -3.6% |
| 2016-07-01 to 2017-01-01 | yield_mom_13w, trend_26w+time_4w | -1.2% |
| 2017-01-01 to 2017-07-01 | yield_mom_13w | -2.5% |
| 2017-07-01 to 2018-01-01 | none qualified | +0.0% |
| 2018-01-01 to 2018-07-01 | none qualified | +0.0% |
| 2018-07-01 to 2019-01-01 | yield_mom_13w | -3.2% |
| 2019-01-01 to 2019-07-01 | yield_mom_13w | -3.0% |
| 2019-07-01 to 2020-01-01 | none qualified | +0.0% |
| 2020-01-01 to 2020-07-01 | none qualified | +0.0% |
| 2020-07-01 to 2021-01-01 | none qualified | +0.0% |
| 2021-01-01 to 2021-07-01 | none qualified | +0.0% |
| 2021-07-01 to 2022-01-01 | yield_mom_13w | -6.5% |
| 2022-01-01 to 2022-07-01 | none qualified | +0.0% |
| 2022-07-01 to 2023-01-01 | yield_mom_13w | -7.3% |
| 2023-01-01 to 2023-07-01 | none qualified | +0.0% |
| 2023-07-01 to 2024-01-01 | yield_mom_13w | -11.5% |
| 2024-01-01 to 2024-07-01 | none qualified | +0.0% |
| 2024-07-01 to 2025-01-01 | none qualified | +0.0% |
| 2025-01-01 to 2025-07-01 | none qualified | +0.0% |
| 2025-07-01 to 2025-10-22 | none qualified | +0.0% |

Out of sample: Sharpe -0.59, t-stat -2.33, -5.9% a year at 10% volatility, worst drop 63%. With higher costs: Sharpe -0.60.

Holdout year (from 2025-10-22): chosen on everything before it: nothing qualified; result +0.0%.

## 5. What Claude debates would cost against what this makes

At 100 USD and 10% volatility, using the out-of-sample result, with a debate at 0.15 USD:

| Debate design | Debates a year | API cost | Expected trading profit | Net | Capital needed to break even |
|---|---|---|---|---|---|
| One debate per pair per week (7 a week) | 364 | 54.60 USD | -5.85 USD | -60.45 USD | never (no edge) |
| One debate a week for the whole portfolio | 52 | 7.80 USD | -5.85 USD | -13.65 USD | never (no edge) |
| One debate a month for the whole portfolio | 12 | 1.80 USD | -5.85 USD | -7.65 USD | never (no edge) |

Whether a debate adds edge on top of a rule can't be backtested honestly: Claude has read about these years, so a debate replayed on 2015 data already knows what happened next. It can only be measured going forward, by logging debate calls next to the plain rule's calls.

## 6. How the desk gets in and out today

- **In:** after a debate, a market order goes in straight away. No limit price, no waiting for a better level.
- **Out:** the head of desk sets a stop loss (always) and maybe a take-profit when the trade opens. OANDA closes the trade when either is hit. The agents also give a 'hold for N hours', but the code only uses it to score their calls, never to close a trade.
- **After entry, nobody looks again.** No agent re-reviews an open trade when news breaks, a central bank moves, or the reason for the trade goes away. The only other exit is the kill switch (budget gone).

## 7. Proposal: managing open trades

What the tests above say about exits:

1. Exits can't rescue a trade idea with no edge. On random entries every exit rule lost about the cost of trading (section 2).
2. No single exit was best for every rule. The right exit depends on why the trade was opened, so it should be decided at entry and written down, not improvised later.
3. Every trade still needs a hard stop at OANDA, because the desk can be offline and markets gap.

So the proposal is a cheap code check with an agent re-review only when something has changed:

| Layer | What it does | Cost |
|---|---|---|
| Stop at OANDA | Every trade keeps its hard stop, set at entry (as today) | free |
| Exit plan at entry | The head of desk must name the exit type (time limit, signal flip, trailing stop, or target) and the reason the trade exists, stored with the trade | already part of the debate |
| Code check every scan | Closes the trade when its exit plan says so: time limit reached, the rule that opened it has flipped, or a trailing stop is hit | free |
| Agent re-review on triggers only | A short two-agent check (hold or close) when a central bank moves against the trade, the price moves more than two typical weekly moves against it, or major news hits the pair | about 0.05 USD each (estimate) |

Central banks for the 7 pairs moved rates about 3.7 times a year per pair since 2004. With 3 trades open, roughly half of those moves going against a trade, and about one other trigger a month per trade, that is about 42 re-reviews a year, or about 2 USD a year of API credits. Re-reviewing every open trade every hour instead would cost about 936 USD a year.

The trigger list itself is untested: central bank moves against a trade made no reliable difference here (section 2, 'central bank' exits), so this layer is a safety net for surprises, not a source of profit.
