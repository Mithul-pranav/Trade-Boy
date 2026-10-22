"""Command line entry point.

    tradeboy check    connect to OANDA and print the account and live prices
    tradeboy resume   clear the kill switch after a human has reviewed what happened
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


def resume() -> None:
    journal = Journal(load_settings().journal_path)
    journal.set_halted(False)
    print("Kill switch cleared. The desk can trade again.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="tradeboy")
    parser.add_argument("command", choices=["check", "resume"])
    args = parser.parse_args()
    {"check": check, "resume": resume}[args.command]()


if __name__ == "__main__":
    main()
