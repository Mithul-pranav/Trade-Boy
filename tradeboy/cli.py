"""Command line entry point.

    tradeboy check    connect to OANDA and print the account and live prices
    tradeboy run      run the desk: scan every 15 minutes and debate what the scanner flags (dry run)
    tradeboy run --paper
                      same, but place the winning trades on the OANDA practice account
    tradeboy debate EUR_USD [--paper]
                      run one debate on a pair now and print it
    tradeboy status   budget, kill switch, API spend, agreement rate and track records
    tradeboy resume   clear the kill switch after Mithul has approved trading again
    tradeboy resume --new-budget
                      also start a fresh budget (capital.starting_amount) from the current account value
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

from .calendar import FeedCalendar
from .news import Feed, NewsFeeds
from .config import ROOT, load_credentials, load_settings
from .journal import Journal
from .oanda import OandaClient


def check() -> None:
    settings = load_settings()
    creds = load_credentials()
    client = OandaClient(creds)
    account = client.account()
    print(f"OANDA {creds.env} account {client.account_id}")
    print(f"  NAV {account.nav:,.2f} {account.currency}, balance {account.balance:,.2f}")
    print(f"  open trades: {len(account.open_trades)}")
    for instrument, q in client.quotes(settings.pairs).items():
        print(f"  {instrument:8} bid {q.bid:<10} ask {q.ask:<10} spread {q.spread_pips:.1f} pips")


def _desk(paper: bool):
    from .desk import Desk
    from .llm import ClaudeLLM, api_key

    if not (api_key() or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        sys.exit("Set TRADEBOY_ANTHROPIC_API_KEY (or ANTHROPIC_API_KEY) in the environment so the agents can reach Claude.")
    settings = load_settings()
    creds = load_credentials()
    if paper and creds.env != "practice":
        sys.exit("--paper only places orders on the OANDA practice account. Real money is a later, separate decision.")
    calendar = FeedCalendar(settings.calendar_url, fail_closed=paper) if settings.calendar_url else None
    news = NewsFeeds([Feed(f["url"], f.get("name", f["url"]), tuple(f.get("currencies", ()))) for f in settings.news_feeds])
    broker = OandaClient(creds)
    return Desk(settings, broker, Journal(settings.journal_path), ClaudeLLM(), calendar, dry_run=not paper,
                news=news if settings.news_feeds else None)


def _setup_logging() -> None:
    log_path = ROOT / "data" / "desk.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(log_path)],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def run(paper: bool, once: bool) -> None:
    _setup_logging()
    desk = _desk(paper)
    interval = desk.settings.scan_interval_minutes * 60
    logging.getLogger("tradeboy").info("Desk started (%s).", "paper trading" if paper else "dry run")
    while True:
        started = time.time()
        try:
            desk.cycle(datetime.now(timezone.utc))
        except Exception:  # one bad cycle (network, API) must not stop a 24/5 desk
            logging.getLogger("tradeboy").exception("Cycle failed")
        if once:
            return
        time.sleep(max(0, interval - (time.time() - started)))


def debate(instrument: str, paper: bool) -> None:
    _setup_logging()
    desk = _desk(paper)
    result = desk.debate(instrument, datetime.now(timezone.utc), "Requested from the command line")
    if result:
        print(json.dumps(result.transcript, indent=2, default=str))


def status() -> None:
    from .executor import Executor

    settings = load_settings()
    journal = Journal(settings.journal_path)
    now = datetime.now(timezone.utc)
    budget = journal.budget()
    print(f"Halted: {'YES, waiting for Mithul' if journal.is_halted() else 'no'}")
    if budget:
        broker = OandaClient(load_credentials())
        account = Executor(settings, broker, journal).budget_account()
        print(f"Budget equity: {account.nav:,.2f} {account.currency} (started at {budget[1]:,.2f})")
    print(f"Claude API spend: {journal.month_spend(now):.2f} USD this month, "
          f"{journal.day_spend(now):.2f} USD in the last 24 hours "
          f"(caps {settings.claude.monthly_budget_usd} / {settings.claude.daily_budget_usd})")
    print(f"Debates today: {journal.debates_today(now)} of {settings.claude.max_debates_per_day}")
    agreement = journal.agreement_rate()
    if agreement:
        print(f"Analysts agreed from the start in {agreement[0]:.0%} of the last {agreement[1]} debates")
    if journal.get_state("agreement_alarm"):
        print(f"AGREEMENT ALARM: {journal.get_state('agreement_alarm')}")
    for agent, record in journal.track_records().items():
        print(f"  {agent:14} {record['scored_calls']} calls, {record['hit_rate_pct']}% right, "
              f"{record['avg_pips']:+} pips on average")


def resume(new_budget: bool) -> None:
    journal = Journal(load_settings().journal_path)
    journal.set_halted(False)
    if new_budget:
        # Dropping the budget makes the executor start a new one on its next run.
        journal.db.execute("DELETE FROM state WHERE key IN ('baseline_nav', 'budget_home')")
        journal.db.commit()
    print("Kill switch cleared. The desk can trade again.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="tradeboy")
    parser.add_argument("command", choices=["check", "run", "debate", "status", "resume"])
    parser.add_argument("instrument", nargs="?", help="with debate: the pair, e.g. EUR_USD")
    parser.add_argument("--paper", action="store_true", help="place trades on the OANDA practice account")
    parser.add_argument("--once", action="store_true", help="with run: one cycle, then exit")
    parser.add_argument("--new-budget", action="store_true", help="with resume: start a fresh budget")
    args = parser.parse_args()
    if args.command == "check":
        check()
    elif args.command == "run":
        run(args.paper, args.once)
    elif args.command == "debate":
        if not args.instrument:
            parser.error("debate needs a pair, e.g. tradeboy debate EUR_USD")
        debate(args.instrument, args.paper)
    elif args.command == "status":
        status()
    else:
        resume(args.new_budget)


if __name__ == "__main__":
    main()
