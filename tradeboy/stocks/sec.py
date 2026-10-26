"""SEC EDGAR: every company's filings, free and timestamped to the second.

An 8-K with item 2.02 is an earnings release; its exhibit 99.1 is the press release itself. 10-Q and
10-K are the quarterly and annual reports. EDGAR asks every script to send a contact in its
User-Agent (TRADEBOY_SEC_USER_AGENT) and to stay under 10 requests a second.
"""

from __future__ import annotations

import html
import os
import re
import time
from datetime import date, datetime, timedelta, timezone

import httpx

from .models import Filing

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{folder}"
UA_VAR = "TRADEBOY_SEC_USER_AGENT"
ITEM_NAMES = {
    "1.01": "material agreement", "1.02": "agreement ended", "2.01": "acquisition or sale completed",
    "2.02": "results of operations (earnings)", "2.05": "restructuring costs", "2.06": "impairment",
    "4.02": "past financials no longer reliable", "5.02": "executive or director change",
    "7.01": "Regulation FD disclosure", "8.01": "other events", "9.01": "exhibits",
}


class SecError(RuntimeError):
    pass


def html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d)>", "\n", raw)
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r" *\n[\s]*", "\n", text).strip()


class Edgar:
    def __init__(self, user_agent: str | None = None, http: httpx.Client | None = None, refresh_minutes: int = 10):
        agent = os.environ.get(UA_VAR) or user_agent or "Trade-Boy research"
        self.http = http or httpx.Client(timeout=20, headers={"User-Agent": agent, "Accept-Encoding": "gzip, deflate"})
        self.refresh = timedelta(minutes=refresh_minutes)
        self._ciks: dict[str, int] = {}
        self._filings: dict[str, tuple[datetime, list[Filing]]] = {}
        self._last_call = 0.0
        self.errors: dict[str, str] = {}

    def _get(self, url: str) -> httpx.Response:
        wait = 0.15 - (time.monotonic() - self._last_call)  # well under EDGAR's 10 requests a second
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()
        try:
            resp = self.http.get(url)
        except httpx.HTTPError as exc:
            raise SecError(f"SEC EDGAR unreachable: {type(exc).__name__}") from None
        if resp.status_code != 200:
            raise SecError(f"SEC EDGAR answered HTTP {resp.status_code} for {url.split('.gov')[-1][:80]}")
        return resp

    def cik(self, symbol: str) -> int:
        if not self._ciks:
            data = self._get(TICKERS_URL).json()
            self._ciks = {row["ticker"].upper(): int(row["cik_str"]) for row in data.values()}
        if symbol.upper() not in self._ciks:
            raise SecError(f"{symbol} is not in the SEC ticker list.")
        return self._ciks[symbol.upper()]

    def filings(self, symbol: str, now: datetime, forms: tuple[str, ...] = ("8-K", "10-Q", "10-K"),
                days: int = 30) -> list[Filing]:
        """Recent filings of these forms, newest first. Cached for a few minutes."""
        cached = self._filings.get(symbol)
        if cached and now - cached[0] < self.refresh:
            return [f for f in cached[1] if f.form in forms]
        try:
            cik = self.cik(symbol)
            recent = self._get(SUBMISSIONS_URL.format(cik=cik)).json()["filings"]["recent"]
            self.errors.pop(symbol, None)
        except (SecError, KeyError, ValueError) as exc:
            self.errors[symbol] = f"SEC filings for {symbol} unavailable: {exc}"
            return [f for f in cached[1] if f.form in forms] if cached else []
        since = (now - timedelta(days=days)).date()
        out = []
        for i, form in enumerate(recent["form"]):
            filed = date.fromisoformat(recent["filingDate"][i])
            if filed < since:
                break  # newest first
            accepted = recent.get("acceptanceDateTime", [None] * (i + 1))[i]
            accession = recent["accessionNumber"][i]
            items = tuple(x.strip() for x in (recent.get("items", [""] * (i + 1))[i] or "").split(",") if x.strip())
            url = ARCHIVE_URL.format(cik=cik, folder=accession.replace("-", "")) + "/" + recent["primaryDocument"][i]
            out.append(Filing(symbol, form, accession, filed,
                              datetime.fromisoformat(accepted.replace("Z", "+00:00")) if accepted else None, items, url))
        self._filings[symbol] = (now, out)
        return [f for f in out if f.form in forms]

    def filing_text(self, filing: Filing, max_chars: int = 12000) -> str:
        """The part of the filing an analyst should read: the press release for an earnings 8-K, else the document."""
        folder = filing.url.rsplit("/", 1)[0]
        url = filing.url
        if filing.form == "8-K":
            try:
                files = self._get(folder + "/index.json").json()["directory"]["item"]
                exhibits = [f["name"] for f in files if re.search(r"ex-?99", f["name"], re.I) and f["name"].endswith((".htm", ".html", ".txt"))]
                if exhibits:
                    url = f"{folder}/{sorted(exhibits)[0]}"
            except (SecError, KeyError, ValueError):
                pass
        text = html_to_text(self._get(url).text)
        if filing.form in ("10-Q", "10-K"):
            # Management's discussion is where the quarter is explained; start there when it can be found.
            m = re.search(r"(?i)item\s*[27]\.?\s*management.s discussion", text[5000:])
            if m:
                text = text[5000 + m.start():]
        return text[:max_chars]

    def gaps(self, symbol: str) -> list[str]:
        return [self.errors[symbol]] if symbol in self.errors else []


def describe(filing: Filing) -> str:
    items = ", ".join(f"{i} {ITEM_NAMES.get(i, '')}".strip() for i in filing.items if i != "9.01")
    when = filing.accepted.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if filing.accepted else filing.filed.isoformat()
    return f"{filing.form} filed {when}" + (f" (items: {items})" if items else "")
