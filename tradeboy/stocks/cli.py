"""Commands for the stocks desk, run as `tradeboy stocks <command>`.

    tradeboy stocks check     connect to the Alpaca paper account, print it and live quotes
    tradeboy stocks events    list the events the scanner sees now (free: no Claude call)
    tradeboy stocks run       run the desk while the market is open (dry run: debates, no orders)
    tradeboy stocks run --paper
                              same, placing orders on the Alpaca PAPER account
    tradeboy stocks debate NVDA [--paper]
                              one debate on a stock now, as if an event had been found
    tradeboy stocks status    budget, kill switch, spend, positions, track records, calibration, execution
    tradeboy stocks health    the quant developer's checks
    tradeboy stocks research-download
                              download daily prices (Yahoo) and earnings dates (SEC) for the research
    tradeboy stocks research  run the post-earnings drift test, write reports/stocks_drift.md
    tradeboy stocks resume [--new-budget]
                              clear the kill switch after Mithul has approved trading again
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timezone

from ..config import ROOT
from ..journal import Journal
from .config import load_stock_settings


def _setup_logging() -> None:
    log_path = ROOT / "data" / "stocks_desk.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(log_path)])
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _parts(settings):
    from .alpaca import AlpacaPaper
    from .news import stock_news
    from .sec import Edgar
    from .yahoo import YahooFinance

    return AlpacaPaper(), Edgar(settings.sec_user_agent), stock_news(settings.universe), YahooFinance()


def _desk(paper: bool):
    from ..llm import ClaudeLLM, api_key
    from .desk import StockDesk
    from .team import QuantSeat

    if not (api_key() or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        sys.exit("Set TRADEBOY_ANTHROPIC_API_KEY so the agents can reach Claude.")
    settings = load_stock_settings()
    broker, edgar, news, yahoo = _parts(settings)
    return StockDesk(settings, broker, Journal(settings.journal_path), ClaudeLLM(), edgar=edgar, news=news,
                     earnings=yahoo, dry_run=not paper, quant=QuantSeat.load())


def check() -> None:
    from .alpaca import AlpacaPaper

    settings = load_stock_settings()
    broker = AlpacaPaper()
    account, clock = broker.account(), broker.clock()
    print(f"Alpaca PAPER account {account['account_number']} ({account['status']})")
    print(f"  equity {account['equity']:,.2f} {account['currency']}, cash {account['cash']:,.2f}, "
          f"shorting {'on' if account['shorting_enabled'] else 'off'}")
    print(f"  market {'OPEN' if clock.is_open else 'closed'}; next open {clock.next_open:%Y-%m-%d %H:%M %Z}")
    for symbol, q in broker.quotes(settings.symbols + [settings.benchmark]).items():
        print(f"  {symbol:6} bid {q.bid:<9.2f} ask {q.ask:<9.2f} last {q.last:<9.2f} spread {q.spread_pct:.3f}%")


def events() -> None:
    from .events import EventScanner

    settings = load_stock_settings()
    broker, edgar, news, _ = _parts(settings)
    journal = Journal(settings.journal_path)
    found = EventScanner(settings, broker, journal, edgar, news).scan(datetime.now(timezone.utc))
    for e in found:
        print(f"  [{e.kind}] {e.summary}")
    print(f"{len(found)} new events." if found else "No new events.")
    for gap in [g for s in settings.symbols for g in edgar.gaps(s)] + news.gaps():
        print(f"  data gap: {gap}")


def run(paper: bool, once: bool) -> None:
    _setup_logging()
    desk = _desk(paper)
    log = logging.getLogger("tradeboy")
    log.info("Stocks desk started (%s).", "PAPER trading" if paper else "dry run")
    if paper:
        desk.alerts.send(
            "stocks_started",
            f"Stocks desk started on the Alpaca PAPER account. Budget equity {desk.executor.equity():,.2f} USD "
            f"({desk.settings.starting_capital:g} USD to start), {desk.settings.risk.risk_per_trade_pct}% risk per trade, "
            f"{len(desk.settings.symbols)} stocks, at most {desk.settings.claude.max_debates_per_day} debates a day.",
            datetime.now(timezone.utc))
    while True:
        started = time.time()
        try:
            desk.cycle(datetime.now(timezone.utc))
        except Exception:  # one bad cycle (network, API) must not stop the desk
            log.exception("Cycle failed")
        if once:
            return
        time.sleep(max(0, desk.settings.scan_interval_minutes * 60 - (time.time() - started)))


def debate(symbol: str, paper: bool) -> None:
    from .models import StockEvent

    _setup_logging()
    desk = _desk(paper)
    symbol = symbol.upper()
    if symbol not in desk.settings.universe:
        sys.exit(f"{symbol} is not in the universe (config/stocks.yaml).")
    now = datetime.now(timezone.utc)
    event = next((e for e in desk.events.scan(now, [symbol])), None) or StockEvent(
        symbol, "manual", 0, f"Debate on {desk.settings.name(symbol)} requested from the command line",
        f"manual:{symbol}:{now.isoformat()}")
    result = desk.debate(event, now)
    if result:
        print(json.dumps(result.transcript, indent=2, default=str))


def status() -> None:
    from .alpaca import AlpacaPaper
    from .executor import StockExecutor
    from .team import calibration, execution_report

    settings = load_stock_settings()
    journal = Journal(settings.journal_path)
    now = datetime.now(timezone.utc)
    print(f"Halted: {'YES, waiting for Mithul' if journal.is_halted() else 'no'}")
    budget = journal.budget()
    if budget:
        executor = StockExecutor(settings, AlpacaPaper(), journal)
        equity = executor.equity()
        api = journal.total_spend()
        print(f"Budget equity {equity:,.2f} USD (started at {budget[1]:,.2f}); trading {equity - budget[1]:+,.2f}, "
              f"Claude API -{api:,.2f}, net after API costs {equity - budget[1] - api:+,.2f} USD")
        for p in executor.open_positions():
            print(f"  {p['symbol']:6} {p['qty']:+g} shares, worth {p['value']:,.2f}, risk at stop {p['risk']:,.2f}")
    print(f"Claude API spend: {journal.month_spend(now):.2f} USD this month, {journal.day_spend(now):.2f} in 24 hours "
          f"(caps {settings.claude.monthly_budget_usd} / {settings.claude.daily_budget_usd})")
    print(f"Debates today: {journal.debates_today(now)} of {settings.claude.max_debates_per_day}")
    agreement = journal.agreement_rate()
    if agreement:
        print(f"Analysts agreed from the start in {agreement[0]:.0%} of the last {agreement[1]} debates")
    for agent, r in journal.track_records().items():
        print(f"  {agent:14} {r['scored_calls']} calls, {r['hit_rate_pct']}% right, {r['avg_pips']:+} bps on average")
    print("Calibration (ML researcher):", json.dumps(calibration(journal), indent=1))
    print("Execution (quant trader):", json.dumps(execution_report(journal)))
    health = journal.get_state("health")
    if health:
        print("Last health check (quant developer):", health)


def health() -> None:
    from .alpaca import AlpacaPaper
    from .team import health_check

    settings = load_stock_settings()
    problems = health_check(settings, AlpacaPaper(), Journal(settings.journal_path), datetime.now(timezone.utc))
    print("\n".join(f"  PROBLEM: {p}" for p in problems) or "All checks passed.")


def research_download() -> None:
    from . import research
    from .sec import Edgar
    from .yahoo import YahooFinance

    settings = load_stock_settings()
    start = date.fromisoformat(str(settings.research.get("history_start", "2008-01-01")))
    research.download(settings, YahooFinance(), Edgar(settings.sec_user_agent), start)
    print(f"Saved to {research.DATA_DIR}")


def research_run() -> None:
    from . import research

    settings = load_stock_settings()
    result = research.run(settings)
    report = ROOT / "reports" / "stocks_drift.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(research.render(result))
    promoted = research.promote(result)
    for c in result["pass_bar"]:
        print(f"  {'pass' if c['passed'] else 'FAIL'}  {c['check']}: {c['got']} (needs {c['needs']})")
    print(f"{'PASSED: promoted to the quant seat' if promoted else 'NOT passed: the quant seat stays silent'}. Report: {report}")


def resume(new_budget: bool) -> None:
    journal = Journal(load_stock_settings().journal_path)
    journal.set_halted(False)
    if new_budget:
        journal.db.execute("DELETE FROM state WHERE key IN ('baseline_nav', 'budget_home')")
        journal.db.commit()
    print("Kill switch cleared. The stocks desk can trade again.")


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="tradeboy stocks")
    parser.add_argument("command", choices=["check", "events", "run", "debate", "status", "health",
                                            "research-download", "research", "resume"])
    parser.add_argument("symbol", nargs="?", help="with debate: the stock, e.g. NVDA")
    parser.add_argument("--paper", action="store_true", help="place orders on the Alpaca PAPER account")
    parser.add_argument("--once", action="store_true", help="with run: one cycle, then exit")
    parser.add_argument("--new-budget", action="store_true", help="with resume: start a fresh budget")
    args = parser.parse_args(argv)
    if args.command == "debate" and not args.symbol:
        parser.error("debate needs a stock, e.g. tradeboy stocks debate NVDA")
    {
        "check": check, "events": events, "status": status, "health": health,
        "research-download": research_download, "research": research_run,
        "run": lambda: run(args.paper, args.once), "debate": lambda: debate(args.symbol, args.paper),
        "resume": lambda: resume(args.new_budget),
    }[args.command]()
