"""Command line entry point.

    tradeboy check    connect to OANDA and print the account and live prices
    tradeboy resume   clear the kill switch after Mithul has approved trading again
    tradeboy resume --new-budget
                      also start a fresh budget (capital.starting_amount) from the current account value
"""

from __future__ import annotations

import argparse

from .config import load_credentials, load_settings
from .journal import Journal
from .oanda import OandaClient


def check() -> None:
    settings = load_settings()
    creds = load_credentials()
    client = OandaClient(creds)
    account = client.account()
    print(f"OANDA {creds.env} account {creds.account_id}")
    print(f"  NAV {account.nav:,.2f} {account.currency}, balance {account.balance:,.2f}")
    print(f"  open trades: {len(account.open_trades)}")
    for instrument, q in client.quotes(settings.pairs).items():
        print(f"  {instrument:8} bid {q.bid:<10} ask {q.ask:<10} spread {q.spread_pips:.1f} pips")


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
    parser.add_argument("command", choices=["check", "resume"])
    parser.add_argument("--new-budget", action="store_true", help="with resume: start a fresh budget")
    args = parser.parse_args()
    if args.command == "check":
        check()
    else:
        resume(args.new_budget)


if __name__ == "__main__":
    main()
